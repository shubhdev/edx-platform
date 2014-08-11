#
# File:   courseware/capa/responsetypes.py
#
"""
Problem response evaluation.  Handles checking of student responses,
of a variety of types.

Used by capa_problem.py
"""

# standard library imports
import abc
import cgi
import inspect
import json
import logging
import html5lib
import numbers
import numpy
import os
from pyparsing import ParseException
import sys
import random
import re
import copy
import requests
import subprocess
import textwrap
import traceback
import xml.sax.saxutils as saxutils
from cmath import isnan
from sys import float_info

from collections import namedtuple
from shapely.geometry import Point, MultiPoint

from dogapi import dog_stats_api

# specific library imports
from calc import evaluator, UndefinedVariable
from . import correctmap
from .registry import TagRegistry
from datetime import datetime
from pytz import UTC
from .util import (
    compare_with_tolerance, contextualize_text, convert_files_to_filenames,
    is_list_of_files, find_with_default, default_tolerance
)
from lxml import etree
from lxml.html.soupparser import fromstring as fromstring_bs     # uses Beautiful Soup!!! FIXME?
import capa.xqueue_interface as xqueue_interface

import capa.safe_exec as safe_exec

log = logging.getLogger(__name__)

registry = TagRegistry()

CorrectMap = correctmap.CorrectMap  # pylint: disable=C0103
CORRECTMAP_PY = None

QUESTION_HINT_CORRECT_STYLE = 'question_hint_correct'
QUESTION_HINT_INCORRECT_STYLE = 'question_hint_incorrect'
QUESTION_HINT_TEXT_STYLE = 'question_hint_text'

#-----------------------------------------------------------------------------
# Exceptions


class LoncapaProblemError(Exception):
    """
    Error in specification of a problem
    """
    pass


class ResponseError(Exception):
    """
    Error for failure in processing a response, including
    exceptions that occur when executing a custom script.
    """
    pass


class StudentInputError(Exception):
    """
    Error for an invalid student input.
    For example, submitting a string when the problem expects a number
    """
    pass

#-----------------------------------------------------------------------------
#
# Main base class for CAPA responsetypes


class LoncapaResponse(object):
    """
    Base class for CAPA responsetypes.  Each response type (ie a capa question,
    which is part of a capa problem) is represented as a subclass,
    which should provide the following methods:

      - get_score           : evaluate the given student answers, and return a CorrectMap
      - get_answers         : provide a dict of the expected answers for this problem

    Each subclass must also define the following attributes:

      - tags                : xhtml tags identifying this response (used in auto-registering)

    In addition, these methods are optional:

      - setup_response : find and note the answer input field IDs for the response; called
                         by __init__

      - check_hint_condition : check to see if the student's answers satisfy a particular
                               condition for a hint to be displayed

      - render_html          : render this Response as HTML (must return XHTML-compliant string)
      - __unicode__          : unicode representation of this Response

    Each response type may also specify the following attributes:

      - max_inputfields      : (int) maximum number of answer input fields (checked in __init__
                               if not None)

      - allowed_inputfields  : list of allowed input fields (each a string) for this Response

      - required_attributes  : list of required attributes (each a string) on the main
                               response XML stanza

      - hint_tag             : xhtml tag identifying hint associated with this response inside
                               hintgroup
    """
    __metaclass__ = abc.ABCMeta  # abc = Abstract Base Class

    tags = None
    hint_tag = None

    max_inputfields = None
    allowed_inputfields = []
    required_attributes = []

    def __init__(self, xml, inputfields, context, system):
        """
        Init is passed the following arguments:

          - xml         : ElementTree of this Response
          - inputfields : ordered list of ElementTrees for each input entry field in this Response
          - context     : script processor context
          - system      : LoncapaSystem instance which provides OS, rendering, and user context

        """
        self.xml = xml
        self.original_xml = copy.deepcopy(xml)      # copy of the original, unaltered XML for the benefit of hints

        self.inputfields = inputfields
        self.context = context
        self.capa_system = system

        self.id = xml.get('id')

        # The LoncapaProblemError messages here do not need to be translated as they are
        # only displayed to the user when settings.DEBUG is True
        for abox in inputfields:
            if abox.tag not in self.allowed_inputfields:
                msg = "%s: cannot have input field %s" % (
                    unicode(self), abox.tag)
                msg += "\nSee XML source line %s" % getattr(
                    xml, 'sourceline', '<unavailable>')
                raise LoncapaProblemError(msg)

        if self.max_inputfields and len(inputfields) > self.max_inputfields:
            msg = "%s: cannot have more than %s input fields" % (
                unicode(self), self.max_inputfields)
            msg += "\nSee XML source line %s" % getattr(
                xml, 'sourceline', '<unavailable>')
            raise LoncapaProblemError(msg)

        for prop in self.required_attributes:
            if not xml.get(prop):
                msg = "Error in problem specification: %s missing required attribute %s" % (
                    unicode(self), prop)
                msg += "\nSee XML source line %s" % getattr(
                    xml, 'sourceline', '<unavailable>')
                raise LoncapaProblemError(msg)

        # ordered list of answer_id values for this response
        self.answer_ids = [x.get('id') for x in self.inputfields]
        if self.max_inputfields == 1:
            # for convenience
            self.answer_id = self.answer_ids[0]

        # map input_id -> maxpoints
        self.maxpoints = dict()
        for inputfield in self.inputfields:
            # By default, each answerfield is worth 1 point
            maxpoints = inputfield.get('points', '1')
            self.maxpoints.update({inputfield.get('id'): int(maxpoints)})

        # dict for default answer map (provided in input elements)
        self.default_answer_map = {}
        for entry in self.inputfields:
            answer = entry.get('correct_answer')
            if answer:
                self.default_answer_map[entry.get(
                    'id')] = contextualize_text(answer, self.context)

        if hasattr(self, 'setup_response'):
            self.setup_response()

    def get_max_score(self):
        """
        Return the total maximum points of all answer fields under this Response
        """
        return sum(self.maxpoints.values())

    def render_html(self, renderer, response_msg=''):
        """
        Return XHTML Element tree representation of this Response.

        Arguments:

          - renderer : procedure which produces HTML given an ElementTree
          - response_msg: a message displayed at the end of the Response
        """
        # render ourself as a <span> + our content
        tree = etree.Element('span')

        # problem author can make this span display:inline
        if self.xml.get('inline', ''):
            tree.set('class', 'inline')

        for item in self.xml:
            # call provided procedure to do the rendering
            item_xhtml = renderer(item)
            if item_xhtml is not None:
                tree.append(item_xhtml)
        tree.tail = self.xml.tail

        # Add a <div> for the message at the end of the response
        if response_msg:
            tree.append(self._render_response_msg_html(response_msg))

        return tree

    def evaluate_answers(self, student_answers, old_cmap):
        """
        Called by capa_problem.LoncapaProblem to evaluate student answers, and to
        generate hints (if any).

        Returns the new CorrectMap, with (correctness,msg,hint,hintmode) for each answer_id.
        """
        new_cmap = self.get_score(student_answers)
        self.get_hints(convert_files_to_filenames(
            student_answers), new_cmap, old_cmap)
        # log.debug('new_cmap = %s' % new_cmap)
        return new_cmap

    def _using_new_style_hints(self):
        """
        Examine the problem's XML tree to determine whether the "new style" hint mechanism is in use.

        Return True if it is in use.
        """
        using_new_style_hints = False  # assume we are not using new style hints
        xproblem_element = self.xml.getroottree().xpath('.')
        schema_version = xproblem_element[0].get('schema')
        if schema_version == 'edXML/1.0':                           # this is the right schema
            using_new_style_hints = True                            # turns out we are using new style hints
        return using_new_style_hints

    def get_compound_condition_hints(self, new_cmap, student_answers):  # pylint: disable=W0613
        """
        Check for any compound condition hints for the current question. If any are found
        and the selection matches the criteria specified, modify 'new_cmap'
        appropriately so that the hint material can be rendered further downstream.

        Return True if any match was found
        """
        return False

    def get_single_choice_hints(self, new_cmap, student_answers):
        """
        Check for any single item hints for the current question. If any are found
        and the selection matches the criteria specified, modify 'new_cmap'
        appropriately so that the hint material can be rendered further downstream.
        """
        pass

    def get_xml_hints(self, student_answers, new_cmap):
        """
        Look to the XML for any hinting which might be need to be displayed to the student.
        If any hint material is discovered 'new_cmap' is modified accordingly for display
        further downstream.

        Return True if new style hints were found
        """
        new_style_hints_found = False
        if len(student_answers) > 0:                        # if the student has supplied at least one selection
            if self._using_new_style_hints():               # if we are using new style hints
                new_style_hints_found = True                # note that new style hints may be used
                if not self.get_compound_condition_hints(new_cmap, student_answers):   # if no compound rules matched
                    self.get_single_choice_hints(new_cmap, student_answers)      # check for any single selection hints
        return new_style_hints_found

    def get_hints(self, student_answers, new_cmap, old_cmap):
        """
        Generate adaptive hints for this problem based on student answers, the old CorrectMap,
        and the new CorrectMap produced by get_score.

        Does not return anything.

        Modifies new_cmap, by adding hints to answer_id entries as appropriate.
        """

        hintfn = None
        hint_function_provided = False
        hintgroup = self.xml.find('hintgroup')
        if hintgroup:
            hintfn = hintgroup.get('hintfn')
            if hintfn:
                hint_function_provided = True

        if hint_function_provided:
            # Hint is determined by a function defined in the <script> context; evaluate
            # that function to obtain list of hint, hintmode for each answer_id.

            # The function should take arguments (answer_ids, student_answers, new_cmap, old_cmap)
            # and it should modify new_cmap as appropriate.

            # We may extend this in the future to add another argument which provides a
            # callback procedure to a social hint generation system.

            global CORRECTMAP_PY
            if CORRECTMAP_PY is None:
                # We need the CorrectMap code for hint functions. No, this is not great.
                CORRECTMAP_PY = inspect.getsource(correctmap)

            code = (
                CORRECTMAP_PY + "\n" +
                self.context['script_code'] + "\n" +
                textwrap.dedent("""
                    new_cmap = CorrectMap()
                    new_cmap.set_dict(new_cmap_dict)
                    old_cmap = CorrectMap()
                    old_cmap.set_dict(old_cmap_dict)
                    {hintfn}(answer_ids, student_answers, new_cmap, old_cmap)
                    new_cmap_dict.update(new_cmap.get_dict())
                    old_cmap_dict.update(old_cmap.get_dict())
                    """).format(hintfn=hintfn)
            )
            globals_dict = {
                'answer_ids': self.answer_ids,
                'student_answers': student_answers,
                'new_cmap_dict': new_cmap.get_dict(),
                'old_cmap_dict': old_cmap.get_dict(),
            }

            try:
                safe_exec.safe_exec(
                    code,
                    globals_dict,
                    python_path=self.context['python_path'],
                    slug=self.id,
                    random_seed=self.context['seed'],
                    unsafely=self.capa_system.can_execute_unsafe_code(),
                )
            except Exception as err:
                _ = self.capa_system.i18n.ugettext
                msg = _('Error {err} in evaluating hint function {hintfn}.').format(err=err, hintfn=hintfn)
                sourcenum = getattr(self.xml, 'sourceline', _('(Source code line unavailable)'))
                msg += "\n" + _("See XML source line {sourcenum}.").format(sourcenum=sourcenum)
                raise ResponseError(msg)

            new_cmap.set_dict(globals_dict['new_cmap_dict'])
        else:                   # no hint function provided
            if not self.get_xml_hints(student_answers, new_cmap):     # if new style hints were not found
                # hint specified by conditions and text dependent on conditions (a-la Loncapa design)
                # see http://help.loncapa.org/cgi-bin/fom?file=291
                #
                # Example:
                #
                # <formularesponse samples="x@-5:5#11" id="11" answer="$answer">
                #   <textline size="25" />
                #   <hintgroup>
                #     <formulahint samples="x@-5:5#11" answer="$wrongans" name="inversegrad"></formulahint>
                #     <hintpart on="inversegrad">
                #       <text>You have inverted the slope in the question.  The slope is
                #             (y2-y1)/(x2 - x1) you have the slope as (x2-x1)/(y2-y1).</text>
                #     </hintpart>
                #   </hintgroup>
                # </formularesponse>

                if (self.hint_tag is not None
                        and hintgroup
                        and hintgroup.find(self.hint_tag) is not None
                        and hasattr(self, 'check_hint_condition')):

                    rephints = hintgroup.findall(self.hint_tag)
                    hints_to_show = self.check_hint_condition(rephints, student_answers)
                    # can be 'on_request' or 'always' (default)

                    hintmode = hintgroup.get('mode', 'always')
                    for hintpart in hintgroup.findall('hintpart'):
                        if hintpart.get('on') in hints_to_show:
                            hint_text = hintpart.find('text').text
                            # make the hint appear after the last answer box in this
                            # response
                            aid = self.answer_ids[-1]
                            new_cmap.set_hint_and_mode(aid, hint_text, hintmode)
                    log.debug('after hint: new_cmap = %s', new_cmap)

    @abc.abstractmethod
    def get_score(self, student_answers):
        """
        Return a CorrectMap for the answers expected vs given.  This includes
        (correctness, npoints, msg) for each answer_id.

        Arguments:
         - student_answers : dict of (answer_id, answer) where answer = student input (string)
        """
        pass

    @abc.abstractmethod
    def get_answers(self):
        """
        Return a dict of (answer_id, answer_text) for each answer for this question.
        """
        pass

    def check_hint_condition(self, hxml_set, student_answers):
        """
        Return a list of hints to show.

          - hxml_set        : list of Element trees, each specifying a condition to be
                              satisfied for a named hint condition

          - student_answers : dict of student answers

        Returns a list of names of hint conditions which were satisfied.  Those are used
        to determine which hints are displayed.
        """
        pass

    def setup_response(self):
        pass

    def __unicode__(self):
        return u'LoncapaProblem Response %s' % self.xml.tag

    def _render_response_msg_html(self, response_msg):
        """ Render a <div> for a message that applies to the entire response.

        *response_msg* is a string, which may contain XHTML markup

        Returns an etree element representing the response message <div> """
        # First try wrapping the text in a <div> and parsing
        # it as an XHTML tree
        try:
            response_msg_div = etree.XML('<div>%s</div>' % str(response_msg))

        # If we can't do that, create the <div> and set the message
        # as the text of the <div>
        except:
            response_msg_div = etree.Element('div')
            response_msg_div.text = str(response_msg)

        # Set the css class of the message <div>
        response_msg_div.set("class", "response_message")

        return response_msg_div

    # These accessor functions allow polymorphic checking of response
    # objects without having to call hasattr() directly.
    def has_mask(self):
        """True if the response has masking."""
        return hasattr(self, '_has_mask')

    def has_shuffle(self):
        """True if the response has a shuffle transformation."""
        return hasattr(self, '_has_shuffle')

    def has_answerpool(self):
        """True if the response has an answer-pool transformation."""
        return hasattr(self, '_has_answerpool')

#-----------------------------------------------------------------------------


@registry.register
class JavascriptResponse(LoncapaResponse):
    """
    This response type is used when the student's answer is graded via
    Javascript using Node.js.
    """

    tags = ['javascriptresponse']
    max_inputfields = 1
    allowed_inputfields = ['javascriptinput']

    def setup_response(self):
        super(JavascriptResponse, self).setup_response()

        # Sets up generator, grader, display, and their dependencies.
        self.parse_xml()

        self.compile_display_javascript()

        self.params = self.extract_params()

        if self.generator:
            self.problem_state = self.generate_problem_state()
        else:
            self.problem_state = None

        self.solution = None

        self.prepare_inputfield()

    def compile_display_javascript(self):

        # TODO FIXME
        # arjun: removing this behavior for now (and likely forever). Keeping
        # until we decide on exactly how to solve this issue. For now, files are
        # manually being compiled to DATA_DIR/js/compiled.

        # latestTimestamp = 0
        # basepath = self.capa_system.filestore.root_path + '/js/'
        # for filename in (self.display_dependencies + [self.display]):
        #    filepath = basepath + filename
        #    timestamp = os.stat(filepath).st_mtime
        #    if timestamp > latestTimestamp:
        #        latestTimestamp = timestamp
        #
        # h = hashlib.md5()
        # h.update(self.answer_id + str(self.display_dependencies))
        # compiled_filename = 'compiled/' + h.hexdigest() + '.js'
        # compiled_filepath = basepath + compiled_filename

        # if not os.path.exists(compiled_filepath) or os.stat(compiled_filepath).st_mtime < latestTimestamp:
        #    outfile = open(compiled_filepath, 'w')
        #    for filename in (self.display_dependencies + [self.display]):
        #        filepath = basepath + filename
        #        infile = open(filepath, 'r')
        #        outfile.write(infile.read())
        #        outfile.write(';\n')
        #        infile.close()
        #    outfile.close()

        # TODO this should also be fixed when the above is fixed.
        filename = self.capa_system.ajax_url.split('/')[-1] + '.js'
        self.display_filename = 'compiled/' + filename

    def parse_xml(self):
        self.generator_xml = self.xml.xpath('//*[@id=$id]//generator',
                                            id=self.xml.get('id'))[0]

        self.grader_xml = self.xml.xpath('//*[@id=$id]//grader',
                                         id=self.xml.get('id'))[0]

        self.display_xml = self.xml.xpath('//*[@id=$id]//display',
                                          id=self.xml.get('id'))[0]

        self.xml.remove(self.generator_xml)
        self.xml.remove(self.grader_xml)
        self.xml.remove(self.display_xml)

        self.generator = self.generator_xml.get("src")
        self.grader = self.grader_xml.get("src")
        self.display = self.display_xml.get("src")

        if self.generator_xml.get("dependencies"):
            self.generator_dependencies = self.generator_xml.get(
                "dependencies").split()
        else:
            self.generator_dependencies = []

        if self.grader_xml.get("dependencies"):
            self.grader_dependencies = self.grader_xml.get(
                "dependencies").split()
        else:
            self.grader_dependencies = []

        if self.display_xml.get("dependencies"):
            self.display_dependencies = self.display_xml.get(
                "dependencies").split()
        else:
            self.display_dependencies = []

        self.display_class = self.display_xml.get("class")

    def get_node_env(self):

        js_dir = os.path.join(self.capa_system.filestore.root_path, 'js')
        tmp_env = os.environ.copy()
        node_path = self.capa_system.node_path + ":" + os.path.normpath(js_dir)
        tmp_env["NODE_PATH"] = node_path
        return tmp_env

    def call_node(self, args):
        # Node.js code is un-sandboxed. If the LoncapaSystem says we aren't
        # allowed to run unsafe code, then stop now.
        if not self.capa_system.can_execute_unsafe_code():
            _ = self.capa_system.i18n.ugettext
            msg = _("Execution of unsafe Javascript code is not allowed.")
            raise LoncapaProblemError(msg)

        subprocess_args = ["node"]
        subprocess_args.extend(args)

        return subprocess.check_output(subprocess_args, env=self.get_node_env())

    def generate_problem_state(self):

        generator_file = os.path.dirname(os.path.normpath(
            __file__)) + '/javascript_problem_generator.js'
        output = self.call_node([generator_file,
                                 self.generator,
                                 json.dumps(self.generator_dependencies),
                                 json.dumps(str(self.context['seed'])),
                                 json.dumps(self.params)]).strip()

        return json.loads(output)

    def extract_params(self):

        params = {}

        for param in self.xml.xpath('//*[@id=$id]//responseparam',
                                    id=self.xml.get('id')):

            raw_param = param.get("value")
            params[param.get("name")] = json.loads(
                contextualize_text(raw_param, self.context))

        return params

    def prepare_inputfield(self):

        for inputfield in self.xml.xpath('//*[@id=$id]//javascriptinput',
                                         id=self.xml.get('id')):

            escapedict = {'"': '&quot;'}

            encoded_params = json.dumps(self.params)
            encoded_params = saxutils.escape(encoded_params, escapedict)
            inputfield.set("params", encoded_params)

            encoded_problem_state = json.dumps(self.problem_state)
            encoded_problem_state = saxutils.escape(encoded_problem_state,
                                                    escapedict)
            inputfield.set("problem_state", encoded_problem_state)

            inputfield.set("display_file", self.display_filename)
            inputfield.set("display_class", self.display_class)

    def get_score(self, student_answers):
        json_submission = student_answers[self.answer_id]
        (all_correct, evaluation, solution) = self.run_grader(json_submission)
        self.solution = solution
        correctness = 'correct' if all_correct else 'incorrect'
        if all_correct:
            points = self.get_max_score()
        else:
            points = 0
        return CorrectMap(self.answer_id, correctness, npoints=points, msg=evaluation)

    def run_grader(self, submission):
        if submission is None or submission == '':
            submission = json.dumps(None)

        grader_file = os.path.dirname(os.path.normpath(
            __file__)) + '/javascript_problem_grader.js'
        outputs = self.call_node([grader_file,
                                  self.grader,
                                  json.dumps(self.grader_dependencies),
                                  submission,
                                  json.dumps(self.problem_state),
                                  json.dumps(self.params)]).split('\n')

        all_correct = json.loads(outputs[0].strip())
        evaluation = outputs[1].strip()
        solution = outputs[2].strip()
        return (all_correct, evaluation, solution)

    def get_answers(self):
        if self.solution is None:
            (_, _, self.solution) = self.run_grader(None)

        return {self.answer_id: self.solution}


#-----------------------------------------------------------------------------
@registry.register
class ChoiceResponse(LoncapaResponse):
    """
    This response type is used when the student chooses from a discrete set of
    choices. Currently, to be marked correct, all "correct" choices must be
    supplied by the student, and no extraneous choices may be included.

    This response type allows for two inputtypes: radiogroups and checkbox
    groups. radiogroups are used when the student should select a single answer,
    and checkbox groups are used when the student may supply 0+ answers.
    Note: it is suggested to include a "None of the above" choice when no
    answer is correct for a checkboxgroup inputtype; this ensures that a student
    must actively mark something to get credit.

    If two choices are marked as correct with a radiogroup, the student will
    have no way to get the answer right.

    TODO: Allow for marking choices as 'optional' and 'required', which would
    not penalize a student for including optional answers and would also allow
    for questions in which the student can supply one out of a set of correct
    answers.This would also allow for survey-style questions in which all
    answers are correct.

    Example:

    <choiceresponse>
        <radiogroup>
            <choice correct="false">
                <text>This is a wrong answer.</text>
            </choice>
            <choice correct="true">
                <text>This is the right answer.</text>
            </choice>
            <choice correct="false">
                <text>This is another wrong answer.</text>
            </choice>
        </radiogroup>
    </choiceresponse>

    In the above example, radiogroup can be replaced with checkboxgroup to allow
    the student to select more than one choice.

    TODO: In order for the inputtypes to render properly, this response type
    must run setup_response prior to the input type rendering. Specifically, the
    choices must be given names. This behavior seems like a leaky abstraction,
    and it'd be nice to change this at some point.

    """
    tags = ['choiceresponse']
    hint_tag = 'choicehint'
    max_inputfields = 1
    allowed_inputfields = ['checkboxgroup', 'radiogroup']
    correct_choices = None

    def setup_response(self):
        super(ChoiceResponse, self).setup_response()

        self.assign_choice_names()

        correct_xml = self.xml.xpath('//*[@id=$id]//choice[@correct="True" or @correct="true"]', id=self.xml.get('id'))

        self.correct_choices = set([choice.get(
            'name') for choice in correct_xml])

    def wrap_hints_correct_or_incorrect(self, new_cmap, problem, problem_hint_shown):
        """
        If any question hints have been added to the 'msg' string in 'new_cmap' wrap that
        html text in a <div> element announcing the correct/incorrect status of the student's
        response.
        :param new_cmap:           The correct map under construction
        :param problem:            The problem id
        :param problem_hint_shown: True if at least one question hint was added that needs wrapping
        :return:                   None
        """
        if problem_hint_shown:
            _ = self.capa_system.i18n.ugettext
            if new_cmap[problem]['correctness'] == 'correct':
                correctness_string = _('CORRECT')
                div_class = 'question_hint_correct'
            else:
                correctness_string = _('INCORRECT')
                div_class = 'question_hint_incorrect'

            new_cmap[problem]['msg'] = '<div class="' + div_class + '">' + \
                                       correctness_string + new_cmap[problem]['msg'] + \
                                       '</div>'.format(div_class, correctness_string, new_cmap[problem]['msg'])

    def get_single_choice_hints(self, new_cmap, student_answers):
        '''
        Check the XML for any hints which should be delivered to the student based
        on the answer choices made.

        :param new_cmap:        the 'correct map' to which applicable hints will be
                                added for display by downstream code
        :param student_answers: the set of answer choices made by the student
        :return:                nothing
        '''
        problem_hint_shown = False
        for problem_id in student_answers:
            if self.answer_id == problem_id:
                student_answer_list = student_answers[problem_id]
                for choice_element in self.xml.findall('checkboxgroup/choice'):
                    hint = ''
                    if choice_element.get('name') in student_answer_list:    # if this choice was selected by student
                        choicehints = choice_element.xpath('./choicehint [@selected="True"]')
                        if choicehints:
                            hint = choicehints[0].text
                    else:
                        choicehints = choice_element.xpath('./choicehint [@selected="False"]')
                        if choicehints:
                            hint = choicehints[0].text

                    if hint:
                        problem_hint_shown = True
                        new_cmap[problem_id]['msg'] += '<div class="' + QUESTION_HINT_TEXT_STYLE + '">' + hint + '</div>'

                self.wrap_hints_correct_or_incorrect(new_cmap, problem_id, problem_hint_shown)

    def assign_choice_names(self):
        """
        Initialize name attributes in <choice> tags for this response.
        """

        for index, choice in enumerate(self.xml.xpath('//*[@id=$id]//choice',
                                                      id=self.xml.get('id'))):
            if not choice.get('id'):
                choice.set("id", chr(ord("A") + index))   # each choice gets a default 'id' of A,B,C...
            choice.set("name", "choice_" + str(index))

    def get_score(self, student_answers):

        student_answer = student_answers.get(self.answer_id, [])

        if not isinstance(student_answer, list):
            student_answer = [student_answer]

        student_answer = set(student_answer)

        required_selected = len(self.correct_choices - student_answer) == 0
        no_extra_selected = len(student_answer - self.correct_choices) == 0

        correct = required_selected & no_extra_selected

        if correct:
            return CorrectMap(self.answer_id, 'correct')
        else:
            return CorrectMap(self.answer_id, 'incorrect')

    def get_answers(self):
        answers = {}
        if self.correct_choices:
            answers = {self.answer_id: list(self.correct_choices)}
        return answers

    def get_compound_condition_hints(self, new_cmap, student_answers):
        """
        Check the XML for any compund condition hints which should be delivered to the student based
        on the answer choices made.

        :param new_cmap:        the 'correct map' to which applicable hints will be
                                added for display by downstream code
        :param student_answers: the set of answer choices made by the student
        :return:                true if at least one compound condition hint matched
        """
        compound_hint_matched = False       # assume we won't find any matching rules

        for student_answer in student_answers:
            if self.answer_id == student_answer:
                problem_hint_shown = False
                selection_id_list = []              # create a list of all the student's selected id's
                for student_answer in student_answers[student_answer]:
                    choice_list = self.xml.xpath('checkboxgroup/choice [@name="' + str(student_answer) + '"]')
                    if choice_list:             # if we found at least one choice element
                        choice = choice_list[0]
                        selection_id_list.append(choice.get('id').upper())
                selection_id_list.sort()        # sort the list to make comparison easier

                for boolean_hint_element in self.xml.xpath("//booleanhint"):
                    boolean_condition_string = boolean_hint_element.get("value").upper()
                    boolean_condition_string = boolean_condition_string.replace("AND", " ")  # delete optional 'AND' operator
                    boolean_condition_string = boolean_condition_string.replace("*", " ")    # delete any '*' operator

                    boolean_condition_list = []
                    for boolean_conditon_token in boolean_condition_string.split(" "):
                        if len(boolean_conditon_token.strip()) > 0:
                            boolean_condition_list.append(boolean_conditon_token)
                    boolean_condition_list.sort()   # sort the list to make comparison easier

                    if boolean_condition_list == selection_id_list:
                        compound_hint_matched = True

                        hint_label = ''
                        if boolean_hint_element.get('label'):
                            hint_label = boolean_hint_element.get('label') + ': '

                        new_cmap[self.answer_id]['msg'] = '<div class="' + QUESTION_HINT_TEXT_STYLE + '">' \
                            + hint_label + boolean_hint_element.text.strip() + '</div>'
                        problem_hint_shown = True
                        break

                self.wrap_hints_correct_or_incorrect(new_cmap, self.answer_id, problem_hint_shown)

        return compound_hint_matched

#-----------------------------------------------------------------------------


@registry.register
class MultipleChoiceResponse(LoncapaResponse):
    """
    Multiple Choice Response
    The shuffle and answer-pool features on this class enable permuting and
    subsetting the choices shown to the student.
    Both features enable name "masking":
    With masking, the regular names of multiplechoice choices
    choice_0 choice_1 ... are not used. Instead we use random masked names
    mask_2 mask_0 ... so that a view-source of the names reveals nothing about
    the original order. We introduce the masked names right at init time, so the
    whole software stack works with just the one system of naming.
    The .has_mask() test on a response checks for masking, implemented by a
    ._has_mask attribute on the response object.
    The logging functionality in capa_base calls the unmask functions here
    to translate back to choice_0 name style for recording in the logs, so
    the logging is in terms of the regular names.
    """
    # TODO: handle direction and randomize

    tags = ['multiplechoiceresponse']
    hint_tag = 'choicehint'
    max_inputfields = 1
    allowed_inputfields = ['choicegroup']
    correct_choices = None

    def setup_response(self):
        super(MultipleChoiceResponse, self).setup_response()

        # call secondary setup for MultipleChoice questions, to set name
        # attributes
        self.mc_setup_response()

        # define correct choices (after calling secondary setup)
        xml = self.xml
        cxml = xml.xpath('//*[@id=$id]//choice', id=xml.get('id'))

        # contextualize correct attribute and then select ones for which
        # correct = "true"
        self.correct_choices = [
            contextualize_text(choice.get('name'), self.context)
            for choice in cxml
            if contextualize_text(choice.get('correct'), self.context).upper() == "TRUE"

        ]

    def get_single_choice_hints(self, new_cmap, student_answers):
        '''
        Check the XML for any hints which should be delivered to the student based
        on the answer choices made.

        :param new_cmap:        the 'correct map' to which applicable hints will be
                                added for display by downstream code
        :param student_answers: the set of answer choices made by the student
        :return:                nothing
        '''

        for student_answer in student_answers:
            if self.answer_id == student_answer:
                choicegroup_test = '[@id="' + student_answer + '"]'
                choice_test = '[@name="' + student_answers[student_answer] + '"]'
                choice = self.xml.xpath('//choicegroup' + choicegroup_test + '/choice' + choice_test)[0]
                choice_hints = self.xml.xpath('//choicegroup' + choicegroup_test + '/choice' + choice_test + '/choicehint')
                if choice_hints:
                    choice_hint = choice_hints[0]
                    choice_hint_text = choice_hint.text.strip()
                    if len(choice_hint_text) > 0:
                        choice_hint_label = choice_hint.get('label')

                        message_style_class = QUESTION_HINT_INCORRECT_STYLE         # assume the answer was incorrect
                        if choice.get('correct').upper() == 'TRUE':
                            message_style_class = QUESTION_HINT_CORRECT_STYLE       # guessed wrong, answer was correct

                        if choice_hint_label:
                            correctness_string = choice_hint_label + ': '
                        else:
                            correctness_string = 'INCORRECT: '  # assume the answer is incorrect
                            if choice.get('correct').upper() == 'TRUE':
                                correctness_string = 'CORRECT: '

                        new_cmap[self.answer_id]['msg'] = new_cmap[self.answer_id]['msg'] + \
                            '<div class="' + message_style_class + '">' \
                            + correctness_string + choice_hint_text + '</div>'
                break

    def mc_setup_response(self):
        """
        Initialize name attributes in <choice> stanzas in the <choicegroup> in this response.
        Masks the choice names if applicable.
        """
        i = 0
        for response in self.xml.xpath("choicegroup"):
            # Is Masking enabled? -- check for shuffle or answer-pool features
            ans_str = response.get("answer-pool")
            # Masking (self._has_mask) is off, to be re-enabled with a future PR.
            rtype = response.get('type')
            if rtype not in ["MultipleChoice"]:
                # force choicegroup to be MultipleChoice if not valid
                response.set("type", "MultipleChoice")
            for choice in list(response):
                # The regular, non-masked name:
                if choice.get("name") is not None:
                    name = "choice_" + choice.get("name")
                else:
                    name = "choice_" + str(i)
                    i += 1
                # If using the masked name, e.g. mask_0, save the regular name
                # to support unmasking later (for the logs).
                if self.has_mask():
                    mask_name = "mask_" + str(mask_ids.pop())
                    self._mask_dict[mask_name] = name
                    choice.set("name", mask_name)
                else:
                    choice.set("name", name)

    def late_transforms(self, problem):
        """
        Rearrangements run late in the __init__ process.
        Cannot do these at response init time, as not enough
        other stuff exists at that time.
        """
        self.do_shuffle(self.xml, problem)
        self.do_answer_pool(self.xml, problem)

    def get_score(self, student_answers):
        """
        grade student response.
        """
        # log.debug('%s: student_answers=%s, correct_choices=%s' % (
        #   unicode(self), student_answers, self.correct_choices))
        if (self.answer_id in student_answers
                and student_answers[self.answer_id] in self.correct_choices):
            return CorrectMap(self.answer_id, 'correct')
        else:
            return CorrectMap(self.answer_id, 'incorrect')

    def get_answers(self):
        return {self.answer_id: self.correct_choices}

    def unmask_name(self, name):
        """
        Given a masked name, e.g. mask_2, returns the regular name, e.g. choice_0.
        Fails with LoncapaProblemError if called on a response that is not masking.
        """
        if not self.has_mask():
            _ = self.capa_system.i18n.ugettext
            # Translators: 'unmask_name' is a method name and should not be translated.
            msg = _("unmask_name called on response that is not masked")
            raise LoncapaProblemError(msg)
        return self._mask_dict[name]

    def unmask_order(self):
        """
        Returns a list of the choice names in the order displayed to the user,
        using the regular (non-masked) names.
        """
        # With masking disabled, this computation remains interesting to see
        # the displayed order, even though there is no unmasking.
        choices = self.xml.xpath('choicegroup/choice')
        return [choice.get("name") for choice in choices]

    def do_shuffle(self, tree, problem):
        """
        For a choicegroup with shuffle="true", shuffles the choices in-place in the given tree
        based on the seed. Otherwise does nothing.
        Raises LoncapaProblemError if both shuffle and answer-pool are active:
        a problem should use one or the other but not both.
        Does nothing if the tree has already been processed.
        """
        # The tree is already pared down to this <multichoiceresponse> so this query just
        # gets the child choicegroup (i.e. no leading //)
        choicegroups = tree.xpath('choicegroup[@shuffle="true"]')
        if choicegroups:
            choicegroup = choicegroups[0]
            if choicegroup.get('answer-pool') is not None:
                _ = self.capa_system.i18n.ugettext
                # Translators: 'shuffle' and 'answer-pool' are attribute names and should not be translated.
                msg = _("Do not use shuffle and answer-pool at the same time")
                raise LoncapaProblemError(msg)
            # Note in the response that shuffling is done.
            # Both to avoid double-processing, and to feed the logs.
            if self.has_shuffle():
                return
            self._has_shuffle = True  # pylint: disable=W0201
            # Move elements from tree to list for shuffling, then put them back.
            ordering = list(choicegroup.getchildren())
            for choice in ordering:
                choicegroup.remove(choice)
            ordering = self.shuffle_choices(ordering, self.get_rng(problem))
            for choice in ordering:
                choicegroup.append(choice)

    def shuffle_choices(self, choices, rng):
        """
        Returns a list of choice nodes with the shuffling done,
        using the provided random number generator.
        Choices with 'fixed'='true' are held back from the shuffle.
        """
        # Separate out a list of the stuff to be shuffled
        # vs. the head/tail of fixed==true choices to be held back from the shuffle.
        # Rare corner case: A fixed==true choice "island" in the middle is lumped in
        # with the tail group of fixed choices.
        # Slightly tricky one-pass implementation using a state machine
        head = []
        middle = []  # only this one gets shuffled
        tail = []
        at_head = True
        for choice in choices:
            if at_head and choice.get('fixed') == 'true':
                head.append(choice)
                continue
            at_head = False
            if choice.get('fixed') == 'true':
                tail.append(choice)
            else:
                middle.append(choice)
        rng.shuffle(middle)
        return head + middle + tail

    def get_rng(self, problem):
        """
        Get the random number generator to be shared by responses
        of the problem, creating it on the problem if needed.
        """
        # Multiple questions in a problem share one random number generator (rng) object
        # stored on the problem. If each question got its own rng, the structure of multiple
        # questions within a problem could appear predictable to the student,
        # e.g. (c) keeps being the correct choice. This is due to the seed being
        # defined at the problem level, so the multiple rng's would be seeded the same.
        # The name _shared_rng begins with an _ to suggest that it is not a facility
        # for general use.
        # pylint: disable=protected-access
        if not hasattr(problem, '_shared_rng'):
            problem._shared_rng = random.Random(self.context['seed'])
        return problem._shared_rng

    def do_answer_pool(self, tree, problem):
        """
        Implements the answer-pool subsetting operation in-place on the tree.
        Allows for problem questions with a pool of answers, from which answer options shown to the student
        and randomly selected so that there is always 1 correct answer and n-1 incorrect answers,
        where the author specifies n as the value of the attribute "answer-pool" within <choicegroup>

        The <choicegroup> tag must have an attribute 'answer-pool' giving the desired
        pool size. If that attribute is zero or not present, no operation is performed.
        Calling this a second time does nothing.
        Raises LoncapaProblemError if the answer-pool value is not an integer,
        or if the number of correct or incorrect choices available is zero.
        """
        choicegroups = tree.xpath("choicegroup[@answer-pool]")
        if choicegroups:
            choicegroup = choicegroups[0]
            num_str = choicegroup.get('answer-pool')
            if num_str == '0':
                return
            try:
                num_choices = int(num_str)
            except ValueError:
                _ = self.capa_system.i18n.ugettext
                # Translators: 'answer-pool' is an attribute name and should not be translated.
                msg = _("answer-pool value should be an integer")
                raise LoncapaProblemError(msg)

            # Note in the response that answerpool is done.
            # Both to avoid double-processing, and to feed the logs.
            if self.has_answerpool():
                return
            self._has_answerpool = True  # pylint: disable=W0201

            choices_list = list(choicegroup.getchildren())

            # Remove all choices in the choices_list (we will add some back in later)
            for choice in choices_list:
                choicegroup.remove(choice)

            rng = self.get_rng(problem)  # random number generator to use
            # Sample from the answer pool to get the subset choices and solution id
            (solution_id, subset_choices) = self.sample_from_answer_pool(choices_list, rng, num_choices)

            # Add back in randomly selected choices
            for choice in subset_choices:
                choicegroup.append(choice)

            # Filter out solutions that don't correspond to the correct answer we selected to show
            # Note that this means that if the user simply provides a <solution> tag, nothing is filtered
            solutionset = choicegroup.xpath('../following-sibling::solutionset')
            if len(solutionset) != 0:
                solutionset = solutionset[0]
                solutions = solutionset.xpath('./solution')
                for solution in solutions:
                    if solution.get('explanation-id') != solution_id:
                        solutionset.remove(solution)

    def sample_from_answer_pool(self, choices, rng, num_pool):
        """
        Takes in:
            1. list of choices
            2. random number generator
            3. the requested size "answer-pool" number, in effect a max

        Returns a tuple with 2 items:
            1. the solution_id corresponding with the chosen correct answer
            2. (subset) list of choice nodes with num-1 incorrect and 1 correct

        Raises an error if the number of correct or incorrect choices is 0.
        """

        correct_choices = []
        incorrect_choices = []

        for choice in choices:
            if choice.get('correct').upper() == 'TRUE':
                correct_choices.append(choice)
            else:
                incorrect_choices.append(choice)
                # In my small test, capa seems to treat the absence of any correct=
                # attribute as equivalent to ="false", so that's what we do here.

        # We raise an error if the problem is highly ill-formed.
        # There must be at least one correct and one incorrect choice.
        # IDEA: perhaps this sort semantic-lint constraint should be generalized to all multichoice
        # not just down in this corner when answer-pool is used.
        # Or perhaps in the overall author workflow, these errors are unhelpful and
        # should all be removed.
        if len(correct_choices) < 1 or len(incorrect_choices) < 1:
            _ = self.capa_system.i18n.ugettext
            # Translators: 'Choicegroup' is an input type and should not be translated.
            msg = _("Choicegroup must include at least 1 correct and 1 incorrect choice")
            raise LoncapaProblemError(msg)

        # Limit the number of incorrect choices to what we actually have
        num_incorrect = num_pool - 1
        num_incorrect = min(num_incorrect, len(incorrect_choices))

        # Select the one correct choice
        index = rng.randint(0, len(correct_choices) - 1)
        correct_choice = correct_choices[index]
        solution_id = correct_choice.get('explanation-id')

        # Put together the result, pushing most of the work onto rng.shuffle()
        subset_choices = [correct_choice]
        rng.shuffle(incorrect_choices)
        subset_choices += incorrect_choices[:num_incorrect]
        rng.shuffle(subset_choices)

        return (solution_id, subset_choices)


@registry.register
class TrueFalseResponse(MultipleChoiceResponse):

    tags = ['truefalseresponse']

    def mc_setup_response(self):
        i = 0
        for response in self.xml.xpath("choicegroup"):
            response.set("type", "TrueFalse")
            for choice in list(response):
                if choice.get("name") is None:
                    choice.set("name", "choice_" + str(i))
                    i += 1
                else:
                    choice.set("name", "choice_" + choice.get("name"))

    def get_score(self, student_answers):
        correct = set(self.correct_choices)
        answers = set(student_answers.get(self.answer_id, []))

        if correct == answers:
            return CorrectMap(self.answer_id, 'correct')

        return CorrectMap(self.answer_id, 'incorrect')

#-----------------------------------------------------------------------------


@registry.register
class OptionResponse(LoncapaResponse):
    """
    TODO: handle direction and randomize
    """

    tags = ['optionresponse']
    hint_tag = 'optionhint'
    allowed_inputfields = ['optioninput']
    answer_fields = None

    def setup_response(self):
        super(OptionResponse, self).setup_response()

        self.answer_fields = self.inputfields

    def get_score(self, student_answers):
        # log.debug('%s: student_answers=%s' % (unicode(self),student_answers))
        cmap = CorrectMap()
        amap = self.get_answers()
        for aid in amap:
            if aid in student_answers and student_answers[aid] == amap[aid]:
                cmap.set(aid, 'correct')
            else:
                cmap.set(aid, 'incorrect')
        return cmap

    def get_answers(self):
        amap = dict([(af.get('id'), contextualize_text(af.get(
            'correct'), self.context)) for af in self.answer_fields])
        # log.debug('%s: expected answers=%s' % (unicode(self),amap))
        return amap

    def get_single_choice_hints(self, new_cmap, student_answers):
        """
        Check the XML for any hints which should be delivered to the student based
        on the answer choices made.

        :param new_cmap:        the 'correct map' to which applicable hints will be
                                added for display by downstream code
        :param student_answers: the set of answer choices made by the student
        :return:                nothing
        """
        for student_answer_id in student_answers:
            if unicode(self.answer_ids[0]) == student_answer_id:
                optiongroup_test = '[@id="' + student_answer_id + '"]'
                for option in self.xml.xpath('//optioninput' + optiongroup_test + '/option'):
                    if unicode(option.text.strip()) == student_answers[student_answer_id]:
                        for option_hint in option.iter('optionhint'):

                            option_hint_text = option_hint.text.strip()
                            if len(option_hint_text) > 0:
                                option_hint_label = option_hint.get('label')

                                message_style_class = QUESTION_HINT_INCORRECT_STYLE         # assume the answer was incorrect
                                if option.get('correct').upper() == 'TRUE':
                                    message_style_class = QUESTION_HINT_CORRECT_STYLE       # guessed wrong, answer was correct

                                if option_hint_label:
                                    correctness_string = option_hint_label + ': '
                                else:
                                    correctness_string = 'INCORRECT: '  # assume the answer is incorrect
                                    if option.get('correct').upper() == 'TRUE':
                                        correctness_string = 'CORRECT: '

                                new_cmap[student_answer_id]['msg'] = new_cmap[student_answer_id]['msg'] + \
                                    '<div class="' + message_style_class + '">' \
                                    + correctness_string + option_hint_text + '</div>'
                break
#-----------------------------------------------------------------------------


@registry.register
class NumericalResponse(LoncapaResponse):
    """
    This response type expects a number or formulaic expression that evaluates
    to a number (e.g. `4+5/2^2`), and accepts with a tolerance.
    """

    tags = ['numericalresponse']
    hint_tag = 'numericalhint'
    allowed_inputfields = ['textline', 'formulaequationinput']
    required_attributes = ['answer']
    max_inputfields = 1

    def __init__(self, *args, **kwargs):
        self.correct_answer = ''
        self.tolerance = default_tolerance
        self.range_tolerance = False
        self.answer_range = self.inclusion = None
        super(NumericalResponse, self).__init__(*args, **kwargs)

    def setup_response(self):
        super(NumericalResponse, self).setup_response()

        xml = self.xml
        context = self.context
        answer = xml.get('answer')

        if answer.startswith(('[', '(')) and answer.endswith((']', ')')):  # range tolerance case
            self.range_tolerance = True
            self.inclusion = (
                True if answer.startswith('[') else False, True if answer.endswith(']') else False
            )
            try:
                self.answer_range = [contextualize_text(x, context) for x in answer[1:-1].split(',')]
                self.correct_answer = answer[0] + self.answer_range[0] + ', ' + self.answer_range[1] + answer[-1]
            except Exception:
                log.debug("Content error--answer '%s' is not a valid range tolerance answer", answer)
                _ = self.capa_system.i18n.ugettext
                raise StudentInputError(
                    _("There was a problem with the staff answer to this problem.")
                )
        else:
            self.correct_answer = contextualize_text(answer, context)

            # Find the tolerance
            tolerance_xml = xml.xpath(
                '//*[@id=$id]//responseparam[@type="tolerance"]/@default',
                id=xml.get('id')
            )
            if tolerance_xml:  # If it isn't an empty list...
                self.tolerance = contextualize_text(tolerance_xml[0], context)

    def get_staff_ans(self, answer):
        """
        Given the staff answer as a string, find its float value.

        Use `evaluator` for this, but for backward compatability, try the
        built-in method `complex` (which used to be the standard).
        """
        try:
            correct_ans = complex(answer)
        except ValueError:
            # When `correct_answer` is not of the form X+Yj, it raises a
            # `ValueError`. Then test if instead it is a math expression.
            # `complex` seems to only generate `ValueErrors`, only catch these.
            try:
                correct_ans = evaluator({}, {}, answer)
            except Exception:
                log.debug("Content error--answer '%s' is not a valid number", answer)
                _ = self.capa_system.i18n.ugettext
                raise StudentInputError(
                    _("There was a problem with the staff answer to this problem.")
                )

        return correct_ans

    def get_score(self, student_answers):
        """
        Grade a numeric response.
        """
        student_answer = student_answers[self.answer_id]

        _ = self.capa_system.i18n.ugettext
        general_exception = StudentInputError(
            _(u"Could not interpret '{student_answer}' as a number.").format(student_answer=cgi.escape(student_answer))
        )

        # Begin `evaluator` block
        # Catch a bunch of exceptions and give nicer messages to the student.
        try:
            student_float = evaluator({}, {}, student_answer)
        except UndefinedVariable as undef_var:
            raise StudentInputError(
                _(u"You may not use variables ({bad_variables}) in numerical problems.").format(bad_variables=undef_var.message)
            )
        except ValueError as val_err:
            if 'factorial' in val_err.message:
                # This is thrown when fact() or factorial() is used in an answer
                #   that evaluates on negative and/or non-integer inputs
                # ve.message will be: `factorial() only accepts integral values` or
                # `factorial() not defined for negative values`
                raise StudentInputError(
                    _("factorial function evaluated outside its domain:"
                      "'{student_answer}'").format(student_answer=cgi.escape(student_answer))
                )
            else:
                raise general_exception
        except ParseException:
            raise StudentInputError(
                _(u"Invalid math syntax: '{student_answer}'").format(student_answer=cgi.escape(student_answer))
            )
        except Exception:
            raise general_exception
        # End `evaluator` block -- we figured out the student's answer!
        if self.range_tolerance:
            if isinstance(student_float, complex):
                raise StudentInputError(_(u"You may not use complex numbers in range tolerance problems"))
            boundaries = []
            for inclusion, answer in zip(self.inclusion, self.answer_range):
                boundary = self.get_staff_ans(answer)
                if boundary.imag != 0:
                    raise StudentInputError(_("There was a problem with the staff answer to this problem: complex boundary."))
                if isnan(boundary):
                    raise StudentInputError(_("There was a problem with the staff answer to this problem: empty boundary."))
                boundaries.append(boundary.real)
                if compare_with_tolerance(
                        student_float,
                        boundary,
                        tolerance=float_info.epsilon,
                        relative_tolerance=True
                ):
                    correct = inclusion
                    break
            else:
                correct = boundaries[0] < student_float < boundaries[1]
        else:
            correct_float = self.get_staff_ans(self.correct_answer)
            correct = compare_with_tolerance(
                student_float, correct_float, self.tolerance
            )
        if correct:
            return CorrectMap(self.answer_id, 'correct')
        else:
            return CorrectMap(self.answer_id, 'incorrect')

    def compare_answer(self, ans1, ans2):
        """
        Outside-facing function that lets us compare two numerical answers,
        with this problem's tolerance.
        """
        return compare_with_tolerance(
            evaluator({}, {}, ans1),
            evaluator({}, {}, ans2),
            self.tolerance
        )

    def validate_answer(self, answer):
        """
        Returns whether this answer is in a valid form.
        """
        try:
            evaluator(dict(), dict(), answer)
            return True
        except (StudentInputError, UndefinedVariable):
            return False

    def get_answers(self):
        return {self.answer_id: self.correct_answer}

    def _get_hint_label(self, hint_element, is_correct):
        """
        Return the appropriate label string for the hint represented by 'hint_element' -- if
        the course author supplied an override label, that will take precedence over the
        defaults of 'CORRECT' and 'INCORRECT'
        :param hint_element: an XML element containing a question hint's specifications
        :param is_correct: True if the choice associated with this hint is a 'correct' choice
        :return: either the override label (if supplied), or one of the default labels
        """
        _ = self.capa_system.i18n.ugettext
        hint_label = hint_element.get('label')
        if hint_label:
            correctness_string = hint_label + ': '
        else:
            # Translators: these correctness_string values indicate to the student whether the answer is correct or not.
            correctness_string = _("INCORRECT: ")   # assume the answer is incorrect
            if is_correct:
                correctness_string = _("CORRECT: ")
        return correctness_string

    def get_single_choice_hints(self, new_cmap, student_answers):
        """
        Check the XML for any hints which should be delivered to the student based
        on the answer choices made.

        :param new_cmap:        the 'correct map' to which applicable hints will be
                                added for display by downstream code
        :param student_answers: the set of answer choices made by the student
        :return:                True if a single choice hint was found
        """
        hint_found = False

        for problem_id in student_answers:
            if self.answer_id == problem_id:
                if new_cmap.cmap[problem_id]['correctness'] == 'correct':  # if the grader liked the student's answer
                    correct_hints = self.original_xml.xpath('//numericalresponse/correcthint')
                    if correct_hints:
                        for correct_hint in correct_hints:
                            correctness_string = self._get_hint_label(correct_hint, True)
                            new_cmap[problem_id]['msg'] += \
                                '<div class="' + QUESTION_HINT_CORRECT_STYLE + '">' + \
                                correctness_string + correct_hint.text.strip() + '</div>'
                            hint_found = True
        return hint_found

    def _test_answer_for_hint(self, new_cmap, hint_element, problem, student_float):
        """
        Calculate whether a student's answer is within tolerance of the expected answer. If it is,
        add the related hint text to the hint message string held in 'new_cmap' for presentation to
        the student.

        :return: True if a question hint was found which matched.
        """
        hint_found = False
        if 'tolerance' in hint_element.attrib:
            tolerance_string = hint_element.attrib['tolerance']
        else:
            tolerance_string = '0'
        answer_string = hint_element.attrib['answer']
        if compare_with_tolerance(student_float, float(answer_string), float(tolerance_string)):
            correctness_string = self._get_hint_label(hint_element, True)
            new_cmap[problem]['msg'] = new_cmap[problem]['msg'] + '<div class="' + QUESTION_HINT_INCORRECT_STYLE + '">' \
                + correctness_string + hint_element.text.strip() + '</div>'
            hint_found = True
        return hint_found

#-----------------------------------------------------------------------------

@registry.register
class StringResponse(LoncapaResponse):
    """
    This response type allows one or more answers.

    Additional answers are added by `additional_answer` tag.
    If `regexp` is in `type` attribute, than answers and hints are treated as regular expressions.

    Examples:
        <stringresponse answer="Michigan">
            <textline size="20" />
        </stringresponse >

        <stringresponse answer="a1" type="ci regexp">
            <additional_answer>\d5</additional_answer>
            <additional_answer>a3</additional_answer>
            <textline size="20"/>
            <hintgroup>
                <stringhint answer="a0" type="ci" name="ha0" />
                <stringhint answer="a4" type="ci" name="ha4" />
                <stringhint answer="^\d" type="ci" name="re1" />
                <hintpart on="ha0">
                    <startouttext />+1<endouttext />
                </hintpart >
                <hintpart on="ha4">
                    <startouttext />-1<endouttext />
                </hintpart >
                <hintpart on="re1">
                    <startouttext />Any number+5<endouttext />
                </hintpart >
            </hintgroup>
        </stringresponse>
    """
    tags = ['stringresponse']
    hint_tag = 'stringhint'
    allowed_inputfields = ['textline']
    required_attributes = ['answer']
    max_inputfields = 1
    correct_answer = []

    def setup_response_backward(self):
        self.correct_answer = [
            contextualize_text(answer, self.context).strip() for answer in self.xml.get('answer').split('_or_')
        ]

    def setup_response(self):
        super(StringResponse, self).setup_response()

        self.backward = '_or_' in self.xml.get('answer').lower()
        self.regexp = False
        self.case_insensitive = False
        if self.xml.get('type') is not None:
            self.regexp = 'regexp' in self.xml.get('type').lower().split(' ')
            self.case_insensitive = 'ci' in self.xml.get('type').lower().split(' ')

        # backward compatibility, can be removed in future, it is up to @Lyla Fisher.
        if self.backward:
            self.setup_response_backward()
            return
        # end of backward compatibility

        correct_answers = [self.xml.get('answer')] + [element.text for element in self.xml.findall('additional_answer')]
        self.correct_answer = [contextualize_text(answer, self.context).strip() for answer in correct_answers]

        # remove additional_answer from xml, otherwise they will be displayed
        for el in self.xml.findall('additional_answer'):
            self.xml.remove(el)

        # remove incorrect_answer from xml, otherwise it will be displayed
        for element in self.xml.findall('incorrect_answer'):
            self.xml.remove(element)

    def get_score(self, student_answers):
        """Grade a string response """
        student_answer = student_answers[self.answer_id].strip()
        correct = self.check_string(self.correct_answer, student_answer)
        return CorrectMap(self.answer_id, 'correct' if correct else 'incorrect')

    def check_string_backward(self, expected, given):
        if self.case_insensitive:
            return given.lower() in [i.lower() for i in expected]
        return given in expected

    def get_single_choice_hints(self, new_cmap, student_answers):
        """
        Check the XML for any hints which should be delivered to the student based
        on the answer choices made.

        :param new_cmap:        the 'correct map' to which applicable hints will be
                                added for display by downstream code
        :param student_answers: the set of answer choices made by the student
        :return:                True if a single choice hint was found
        """
        hint_found = False
        for problem in student_answers:
            student_answer = student_answers[problem]

            # check the primary answer first
            for primary_answer in self.original_xml.xpath('//stringresponse'):
                if self._check_hint_condition_match(primary_answer.get('answer'), student_answer, self.regexp):
                    hint_found = True
                    for primary_hint in self.original_xml.xpath('//stringresponse/correcthint'):
                        new_cmap[problem]['msg'] = new_cmap[problem]['msg'] \
                            + '<div class="' + QUESTION_HINT_CORRECT_STYLE \
                            + '">CORRECT: ' + primary_hint.text.strip() + '</div>'

            # check all additional answers
            if not hint_found:
                for additional_answer in self.original_xml.xpath('//additional_answer'):
                    additional_answer_text = additional_answer.get("answer")
                    if self._check_hint_condition_match(additional_answer_text, student_answer, self.regexp):
                        hint_found = True
                        for additional_answer_hint in self.original_xml.xpath('//stringresponse/additional_answer'):
                            new_cmap[problem]['msg'] = new_cmap[problem]['msg'] \
                                + '<div class="' + QUESTION_HINT_CORRECT_STYLE \
                                + '">CORRECT: ' + additional_answer_hint.text.strip() + '</div>'

            # check all incorrect answers (regex not allowed)
            if not hint_found:
                for incorrect_answer in self.original_xml.xpath('//stringequalhint'):
                    incorrect_answer_text = incorrect_answer.get("answer")
                    if self._check_hint_condition_match(incorrect_answer_text, student_answer, False):
                        hint_found = True
                        for incorrect_answer_hint in self.original_xml.xpath('//stringequalhint'):
                            new_cmap[problem]['msg'] = new_cmap[problem]['msg'] \
                                + '<div class="' + QUESTION_HINT_INCORRECT_STYLE \
                                + '>INCORRECT: ' + incorrect_answer_hint.text.strip() + '</div>'

            # check all incorrect answers (regex supplied)
            if not hint_found:
                for incorrect_answer in self.original_xml.xpath('//regexphint'):
                    incorrect_answer_text = incorrect_answer.get("answer")
                    if self._check_hint_condition_match(incorrect_answer_text, student_answer, True):
                        hint_found = True
                        attribute_test = '[@answer="' + incorrect_answer_text + '"]'
                        for incorrect_answer_hint in self.original_xml.xpath('//regexphint' + attribute_test):
                            new_cmap[problem]['msg'] = new_cmap[problem]['msg'] \
                                + '<div class="' + QUESTION_HINT_INCORRECT_STYLE \
                                + '>INCORRECT: ' + incorrect_answer_hint.text.strip() + '</div>'

        return hint_found

    def _check_hint_condition_match(self, pattern, answer, use_regex):
        """
        Attempt to match a regular expression against the student answer. Return True if a match is made.
        :param regex:   regular expression to use in attempting the match
        :param answer:  student's answer string
        :return:        True if the expression matches
        """
        result = False
        if answer:
            if isinstance(answer, basestring):              # force answer to be a list
                answer = list(answer)

            _ = self.capa_system.i18n.ugettext
            if use_regex:
                try:
                    flags = re.IGNORECASE if self.case_insensitive else 0
                    regexp = re.compile(pattern.strip(), flags=flags | re.UNICODE)
                    result = bool(re.search(regexp, answer[0].strip()))
                except Exception:
                    msg = _("Illegal regex expression: ") + pattern
                    raise ResponseError(msg)
            else:
                result = unicode(pattern.upper() in answer[0].upper().strip())
        return result

    def check_string(self, expected, given):
        """
        Find given in expected.

        If self.regexp is true, regular expression search is used.
        if self.case_insensitive is true, case insensitive search is used, otherwise case sensitive search is used.
        Spaces around values of attributes are stripped in XML parsing step.

        Args:
            expected: list.
            given: str.

        Returns: bool

        Raises: `ResponseError` if it fails to compile regular expression.

        Note: for old code, which supports _or_ separator, we add some  backward compatibility handling.
        Should be removed soon. When to remove it, is up to Lyla Fisher.
        """
        _ = self.capa_system.i18n.ugettext
        # backward compatibility, should be removed in future.
        if self.backward:
            return self.check_string_backward(expected, given)
        # end of backward compatibility

        if self.regexp:  # regexp match
            flags = re.IGNORECASE if self.case_insensitive else 0
            try:
                regexp = re.compile('^' + '|'.join(expected) + '$', flags=flags | re.UNICODE)
                result = re.search(regexp, given)
            except Exception as err:
                msg = u'[courseware.capa.responsetypes.stringresponse] {error}: {message}'.format(
                    error=_('error'),
                    message=err.message
                )
                log.error(msg, exc_info=True)
                raise ResponseError(msg)
            return bool(result)
        else:  # string match
            if self.case_insensitive:
                return given.lower() in [i.lower() for i in expected]
            else:
                return given in expected

    def check_hint_condition(self, hxml_set, student_answers):
        given = student_answers[self.answer_id].strip()
        hints_to_show = []
        for hxml in hxml_set:
            name = hxml.get('name')

            hinted_answer = contextualize_text(hxml.get('answer'), self.context).strip()

            if self.check_string([hinted_answer], given):
                hints_to_show.append(name)
        log.debug('hints_to_show = %s', hints_to_show)
        return hints_to_show

    def get_answers(self):
        _ = self.capa_system.i18n.ugettext
        # Translators: Separator used in StringResponse to display multiple answers. Example: "Answer: Answer_1 or Answer_2 or Answer_3".
        separator = u' <b>{}</b> '.format(_('or'))
        return {self.answer_id: separator.join(self.correct_answer)}

#-----------------------------------------------------------------------------


@registry.register
class CustomResponse(LoncapaResponse):
    """
    Custom response.  The python code to be run should be in <answer>...</answer>
    or in a <script>...</script>
    """

    tags = ['customresponse']

    allowed_inputfields = ['textline', 'textbox', 'crystallography',
                           'chemicalequationinput', 'vsepr_input',
                           'drag_and_drop_input', 'editamoleculeinput',
                           'designprotein2dinput', 'editageneinput',
                           'annotationinput', 'jsinput', 'formulaequationinput']
    code = None
    expect = None

    def setup_response(self):
        super(CustomResponse, self).setup_response()

        xml = self.xml

        # if <customresponse> has an "expect" (or "answer") attribute then save
        # that
        self.expect = xml.get('expect') or xml.get('answer')

        log.debug('answer_ids=%s', self.answer_ids)

        # the <answer>...</answer> stanza should be local to the current <customresponse>.
        # So try looking there first.
        self.code = None
        answer = None
        try:
            answer = xml.xpath('//*[@id=$id]//answer', id=xml.get('id'))[0]
        except IndexError:
            # print "xml = ",etree.tostring(xml,pretty_print=True)

            # if we have a "cfn" attribute then look for the function specified by cfn, in
            # the problem context ie the comparison function is defined in the
            # <script>...</script> stanza instead
            cfn = xml.get('cfn')
            if cfn:
                log.debug("cfn = %s", cfn)

                # This is a bit twisty.  We used to grab the cfn function from
                # the context, but now that we sandbox Python execution, we
                # can't get functions from previous executions.  So we make an
                # actual function that will re-execute the original script,
                # and invoke the function with the data needed.
                def make_check_function(script_code, cfn):
                    def check_function(expect, ans, **kwargs):
                        extra_args = "".join(", {0}={0}".format(k) for k in kwargs)
                        code = (
                            script_code + "\n" +
                            "cfn_return = %s(expect, ans%s)\n" % (cfn, extra_args)
                        )
                        globals_dict = {
                            'expect': expect,
                            'ans': ans,
                        }
                        globals_dict.update(kwargs)
                        safe_exec.safe_exec(
                            code,
                            globals_dict,
                            python_path=self.context['python_path'],
                            slug=self.id,
                            random_seed=self.context['seed'],
                            unsafely=self.capa_system.can_execute_unsafe_code(),
                        )
                        return globals_dict['cfn_return']
                    return check_function

                self.code = make_check_function(self.context['script_code'], cfn)

        if not self.code:
            if answer is None:
                log.error("[courseware.capa.responsetypes.customresponse] missing"
                          " code checking script! id=%s", self.id)
                self.code = ''
            else:
                answer_src = answer.get('src')
                if answer_src is not None:
                    # TODO: this code seems not to be used any more since self.capa_system.filesystem doesn't exist.
                    self.code = self.capa_system.filesystem.open('src/' + answer_src).read()
                else:
                    self.code = answer.text

    def get_score(self, student_answers):
        """
        student_answers is a dict with everything from request.POST, but with the first part
        of each key removed (the string before the first "_").
        """
        _ = self.capa_system.i18n.ugettext

        log.debug('%s: student_answers=%s', unicode(self), student_answers)

        # ordered list of answer id's
        idset = sorted(self.answer_ids)
        try:
            # ordered list of answers
            submission = [student_answers[k] for k in idset]
        except Exception as err:
            msg = u"[courseware.capa.responsetypes.customresponse] {message}\n idset = {idset}, error = {err}".format(
                message=_("error getting answer from {student_answers}").format(student_answers=student_answers),
                idset=idset,
                err=err
            )

            log.error(
                "[courseware.capa.responsetypes.customresponse] error getting"
                " student answer from %s"
                "\n idset = %s, error = %s",
                student_answers, idset, err
            )
            raise Exception(msg)

        # global variable in context which holds the Presentation MathML from dynamic math input
        # ordered list of dynamath responses
        dynamath = [student_answers.get(k + '_dynamath', None) for k in idset]

        # if there is only one box, and it's empty, then don't evaluate
        if len(idset) == 1 and not submission[0]:
            # default to no error message on empty answer (to be consistent with other
            # responsetypes) but allow author to still have the old behavior by setting
            # empty_answer_err attribute
            msg = (u'<span class="inline-error">{0}</span>'.format(_(u'No answer entered!'))
                   if self.xml.get('empty_answer_err') else '')
            return CorrectMap(idset[0], 'incorrect', msg=msg)

        # NOTE: correct = 'unknown' could be dangerous. Inputtypes such as textline are
        # not expecting 'unknown's
        correct = ['unknown'] * len(idset)
        messages = [''] * len(idset)
        overall_message = ""

        # put these in the context of the check function evaluator
        # note that this doesn't help the "cfn" version - only the exec version
        self.context.update({
            # my ID
            'response_id': self.id,

            # expected answer (if given as attribute)
            'expect': self.expect,

            # ordered list of student answers from entry boxes in our subtree
            'submission': submission,

            # ordered list of ID's of all entry boxes in our subtree
            'idset': idset,

            # ordered list of all javascript inputs in our subtree
            'dynamath': dynamath,

            # dict of student's responses, with keys being entry box IDs
            'answers': student_answers,

            # the list to be filled in by the check function
            'correct': correct,

            # the list of messages to be filled in by the check function
            'messages': messages,

            # a message that applies to the entire response
            # instead of a particular input
            'overall_message': overall_message,

            # any options to be passed to the cfn
            'options': self.xml.get('options'),
            'testdat': 'hello world',
        })

        # Pass DEBUG to the check function.
        self.context['debug'] = self.capa_system.DEBUG

        # Run the check function
        self.execute_check_function(idset, submission)

        # build map giving "correct"ness of the answer(s)
        correct = self.context['correct']
        messages = self.context['messages']
        overall_message = self.clean_message_html(self.context['overall_message'])
        correct_map = CorrectMap()
        correct_map.set_overall_message(overall_message)

        for k in range(len(idset)):
            npoints = self.maxpoints[idset[k]] if correct[k] == 'correct' else 0
            correct_map.set(idset[k], correct[k], msg=messages[k],
                            npoints=npoints)
        return correct_map

    def execute_check_function(self, idset, submission):
        # exec the check function
        if isinstance(self.code, basestring):
            try:
                safe_exec.safe_exec(
                    self.code,
                    self.context,
                    cache=self.capa_system.cache,
                    slug=self.id,
                    random_seed=self.context['seed'],
                    unsafely=self.capa_system.can_execute_unsafe_code(),
                )
            except Exception as err:
                self._handle_exec_exception(err)

        else:
            # self.code is not a string; it's a function we created earlier.

            # this is an interface to the Tutor2 check functions
            fn = self.code
            answer_given = submission[0] if (len(idset) == 1) else submission
            kwnames = self.xml.get("cfn_extra_args", "").split()
            kwargs = {n: self.context.get(n) for n in kwnames}
            log.debug(" submission = %s", submission)
            try:
                ret = fn(self.expect, answer_given, **kwargs)
            except Exception as err:  # pylint: disable=broad-except
                self._handle_exec_exception(err)
            log.debug(
                "[courseware.capa.responsetypes.customresponse.get_score] ret = %s",
                ret
            )
            if isinstance(ret, dict):
                # One kind of dictionary the check function can return has the
                # form {'ok': BOOLEAN, 'msg': STRING}
                # If there are multiple inputs, they all get marked
                # to the same correct/incorrect value
                if 'ok' in ret:
                    correct = ['correct' if ret['ok'] else 'incorrect'] * len(idset)
                    msg = ret.get('msg', None)
                    msg = self.clean_message_html(msg)

                    # If there is only one input, apply the message to that input
                    # Otherwise, apply the message to the whole problem
                    if len(idset) > 1:
                        self.context['overall_message'] = msg
                    else:
                        self.context['messages'][0] = msg

                # Another kind of dictionary the check function can return has
                # the form:
                # {'overall_message': STRING,
                #  'input_list': [{ 'ok': BOOLEAN, 'msg': STRING }, ...] }
                #
                # This allows the function to return an 'overall message'
                # that applies to the entire problem, as well as correct/incorrect
                # status and messages for individual inputs
                elif 'input_list' in ret:
                    overall_message = ret.get('overall_message', '')
                    input_list = ret['input_list']

                    correct = []
                    messages = []
                    for input_dict in input_list:
                        correct.append('correct'
                                       if input_dict['ok'] else 'incorrect')
                        msg = (self.clean_message_html(input_dict['msg'])
                               if 'msg' in input_dict else None)
                        messages.append(msg)
                    self.context['messages'] = messages
                    self.context['overall_message'] = overall_message

                # Otherwise, we do not recognize the dictionary
                # Raise an exception
                else:
                    log.error(traceback.format_exc())
                    _ = self.capa_system.i18n.ugettext
                    raise ResponseError(
                        _("CustomResponse: check function returned an invalid dictionary!")
                    )

            else:
                correct = ['correct' if ret else 'incorrect'] * len(idset)

            self.context['correct'] = correct

    def clean_message_html(self, msg):

        # If *msg* is an empty string, then the code below
        # will return "</html>".  To avoid this, we first check
        # that *msg* is a non-empty string.
        if msg:

            # When we parse *msg* using etree, there needs to be a root
            # element, so we wrap the *msg* text in <html> tags
            msg = '<html>' + msg + '</html>'

            # Replace < characters
            msg = msg.replace('&#60;', '&lt;')

            # Use etree to prettify the HTML
            msg = etree.tostring(fromstring_bs(msg, convertEntities=None),
                                 pretty_print=True)

            msg = msg.replace('&#13;', '')

            # Remove the <html> tags we introduced earlier, so we're
            # left with just the prettified message markup
            msg = re.sub('(?ms)<html>(.*)</html>', '\\1', msg)

            # Strip leading and trailing whitespace
            return msg.strip()

        # If we start with an empty string, then return an empty string
        else:
            return ""

    def get_answers(self):
        """
        Give correct answer expected for this response.

        use default_answer_map from entry elements (eg textline),
        when this response has multiple entry objects.

        but for simplicity, if an "expect" attribute was given by the content author
        ie <customresponse expect="foo" ...> then that.
        """
        if len(self.answer_ids) > 1:
            return self.default_answer_map
        if self.expect:
            return {self.answer_ids[0]: self.expect}
        return self.default_answer_map

    def _handle_exec_exception(self, err):
        """
        Handle an exception raised during the execution of
        custom Python code.

        Raises a ResponseError
        """

        # Log the error if we are debugging
        msg = 'Error occurred while evaluating CustomResponse'
        log.warning(msg, exc_info=True)

        # Notify student with a student input error
        _, _, traceback_obj = sys.exc_info()
        raise ResponseError(err.message, traceback_obj)

#-----------------------------------------------------------------------------


@registry.register
class SymbolicResponse(CustomResponse):
    """
    Symbolic math response checking, using symmath library.
    """

    tags = ['symbolicresponse']
    max_inputfields = 1

    def setup_response(self):
        super(SymbolicResponse, self).setup_response()

        # Symbolic response always uses symmath_check()
        # If the XML did not specify this, then set it now
        # Otherwise, we get an error from the superclass
        self.xml.set('cfn', 'symmath_check')

        # Let CustomResponse do its setup
        super(SymbolicResponse, self).setup_response()

    def execute_check_function(self, idset, submission):
        from symmath import symmath_check
        try:
            # Since we have limited max_inputfields to 1,
            # we can assume that there is only one submission
            answer_given = submission[0]

            ret = symmath_check(
                self.expect, answer_given,
                dynamath=self.context.get('dynamath'),
                options=self.context.get('options'),
                debug=self.context.get('debug'),
            )
        except Exception as err:
            log.error("oops in SymbolicResponse (cfn) error %s", err)
            log.error(traceback.format_exc())
            _ = self.capa_system.i18n.ugettext
            # Translators: 'SymbolicResponse' is a problem type and should not be translated.
            msg = _(u"An error occurred with SymbolicResponse. The error was: {error_msg}").format(
                error_msg=err,
            )
            raise Exception(msg)
        self.context['messages'][0] = self.clean_message_html(ret['msg'])
        self.context['correct'] = ['correct' if ret['ok'] else 'incorrect'] * len(idset)

#-----------------------------------------------------------------------------

## ScoreMessage named tuple ##
## valid:       Flag indicating valid score_msg format (Boolean)
## correct:     Correctness of submission (Boolean)
## score:       Points to be assigned (numeric, can be float)
## msg:         Message from grader to display to student (string)

ScoreMessage = namedtuple('ScoreMessage', ['valid', 'correct', 'points', 'msg'])  # pylint: disable=invalid-name


@registry.register
class CodeResponse(LoncapaResponse):
    """
    Grade student code using an external queueing server, called 'xqueue'.

    Expects 'xqueue' dict in LoncapaSystem with the following keys that are
    needed by CodeResponse::

        capa_system.xqueue = {
            'interface': XQueueInterface object.
            'construct_callback': Per-StudentModule callback URL constructor,
                defaults to using 'score_update' as the correct dispatch (function).
            'default_queuename': Default queue name to submit request (string).
        }

    External requests are only submitted for student submission grading, not
    for getting reference answers.

    """

    tags = ['coderesponse']
    allowed_inputfields = ['textbox', 'filesubmission', 'matlabinput']
    max_inputfields = 1
    payload = None
    initial_display = None
    url = None
    answer = None
    queue_name = None

    def setup_response(self):
        """
        Configure CodeResponse from XML. Supports both CodeResponse and ExternalResponse XML

        TODO: Determines whether in synchronous or asynchronous (queued) mode
        """
        super(CodeResponse, self).setup_response()

        xml = self.xml
        # TODO: XML can override external resource (grader/queue) URL
        self.url = xml.get('url', None)

        # We do not support xqueue within Studio.
        if self.capa_system.xqueue is not None:
            default_queuename = self.capa_system.xqueue['default_queuename']
        else:
            default_queuename = None
        self.queue_name = xml.get('queuename', default_queuename)

        # VS[compat]:
        # Check if XML uses the ExternalResponse format or the generic
        # CodeResponse format
        codeparam = self.xml.find('codeparam')
        assert codeparam is not None, "Unsupported old format! <coderesponse> without <codeparam>"
        self._parse_coderesponse_xml(codeparam)

    def _parse_coderesponse_xml(self, codeparam):
        """
        Parse the new CodeResponse XML format. When successful, sets:
            self.initial_display
            self.answer (an answer to display to the student in the LMS)
            self.payload
        """
        grader_payload = codeparam.find('grader_payload')
        grader_payload = grader_payload.text if grader_payload is not None else ''
        self.payload = {
            'grader_payload': grader_payload,
        }

        # matlab api key can be defined in course settings. if so, add it to the grader payload
        api_key = getattr(self.capa_system, 'matlab_api_key', None)
        if self.xml.find('matlabinput') and api_key:
            self.payload['token'] = api_key
            self.payload['endpoint_version'] = "2"
            self.payload['requestor_id'] = self.capa_system.anonymous_student_id

        self.initial_display = find_with_default(
            codeparam, 'initial_display', '')
        _ = self.capa_system.i18n.ugettext
        self.answer = find_with_default(codeparam, 'answer_display',
                                        _(u'No answer provided.'))

    def get_score(self, student_answers):
        _ = self.capa_system.i18n.ugettext
        try:
            # Note that submission can be a file
            submission = student_answers[self.answer_id]
        except Exception as err:
            log.error(
                'Error in CodeResponse %s: cannot get student answer for %s;'
                ' student_answers=%s',
                err, self.answer_id, convert_files_to_filenames(student_answers)
            )
            raise Exception(err)

        # We do not support xqueue within Studio.
        if self.capa_system.xqueue is None:
            cmap = CorrectMap()
            cmap.set(self.answer_id, queuestate=None,
                     msg=_(u'Error: No grader has been set up for this problem.'))
            return cmap

        # Prepare xqueue request
        #------------------------------------------------------------

        qinterface = self.capa_system.xqueue['interface']
        qtime = datetime.strftime(datetime.now(UTC), xqueue_interface.dateformat)

        anonymous_student_id = self.capa_system.anonymous_student_id

        # Generate header
        queuekey = xqueue_interface.make_hashkey(
            str(self.capa_system.seed) + qtime + anonymous_student_id + self.answer_id
        )
        callback_url = self.capa_system.xqueue['construct_callback']()
        xheader = xqueue_interface.make_xheader(
            lms_callback_url=callback_url,
            lms_key=queuekey,
            queue_name=self.queue_name
        )

        # Generate body
        if is_list_of_files(submission):
            # TODO: Get S3 pointer from the Queue
            self.context.update({'submission': ''})
        else:
            self.context.update({'submission': submission})

        contents = self.payload.copy()

        # Metadata related to the student submission revealed to the external
        # grader
        student_info = {
            'anonymous_student_id': anonymous_student_id,
            'submission_time': qtime,
        }
        contents.update({'student_info': json.dumps(student_info)})

        # Submit request. When successful, 'msg' is the prior length of the
        # queue

        if is_list_of_files(submission):
            # TODO: Is there any information we want to send here?
            contents.update({'student_response': ''})
            (error, msg) = qinterface.send_to_queue(header=xheader,
                                                    body=json.dumps(contents),
                                                    files_to_upload=submission)
        else:
            contents.update({'student_response': submission})
            (error, msg) = qinterface.send_to_queue(header=xheader,
                                                    body=json.dumps(contents))

        # State associated with the queueing request
        queuestate = {'key': queuekey,
                      'time': qtime, }

        cmap = CorrectMap()
        if error:
            _ = self.capa_system.i18n.ugettext
            error_msg = _('Unable to deliver your submission to grader (Reason: {error_msg}).'
                          ' Please try again later.').format(error_msg=msg)
            cmap.set(self.answer_id, queuestate=None, msg=error_msg)
        else:
            # Queueing mechanism flags:
            #   1) Backend: Non-null CorrectMap['queuestate'] indicates that
            #      the problem has been queued
            #   2) Frontend: correctness='incomplete' eventually trickles down
            #      through inputtypes.textbox and .filesubmission to inform the
            #      browser to poll the LMS
            cmap.set(self.answer_id, queuestate=queuestate,
                     correctness='incomplete', msg=msg)

        return cmap

    def update_score(self, score_msg, oldcmap, queuekey):
        """Updates the user's score based on the returned message from the grader."""
        (valid_score_msg, correct, points, msg) = self._parse_score_msg(score_msg)

        _ = self.capa_system.i18n.ugettext

        dog_stats_api.increment(xqueue_interface.XQUEUE_METRIC_NAME, tags=[
            'action:update_score',
            'correct:{}'.format(correct)
        ])

        dog_stats_api.histogram(xqueue_interface.XQUEUE_METRIC_NAME + '.update_score.points_earned', points)

        if not valid_score_msg:
            # Translators: 'grader' refers to the edX automatic code grader.
            error_msg = _('Invalid grader reply. Please contact the course staff.')
            oldcmap.set(self.answer_id, msg=error_msg)
            return oldcmap

        correctness = 'correct' if correct else 'incorrect'

        # TODO: Find out how this is used elsewhere, if any
        self.context['correct'] = correctness

        # Replace 'oldcmap' with new grading results if queuekey matches.  If queuekey
        # does not match, we keep waiting for the score_msg whose key actually
        # matches
        if oldcmap.is_right_queuekey(self.answer_id, queuekey):
            # Sanity check on returned points
            if points < 0:
                points = 0
            # Queuestate is consumed
            oldcmap.set(
                self.answer_id, npoints=points, correctness=correctness,
                msg=msg.replace('&nbsp;', '&#160;'), queuestate=None)
        else:
            log.debug(
                'CodeResponse: queuekey %s does not match for answer_id=%s.',
                queuekey,
                self.answer_id
            )

        return oldcmap

    def get_answers(self):
        anshtml = '<span class="code-answer"><pre><code>%s</code></pre></span>' % self.answer
        return {self.answer_id: anshtml}

    def get_initial_display(self):
        """
        The course author can specify an initial display
        to be displayed the code response box.
        """
        return {self.answer_id: self.initial_display}

    def _parse_score_msg(self, score_msg):
        """
         Grader reply is a JSON-dump of the following dict
           { 'correct': True/False,
             'score': Numeric value (floating point is okay) to assign to answer
             'msg': grader_msg }

        Returns (valid_score_msg, correct, score, msg):
            valid_score_msg: Flag indicating valid score_msg format (Boolean)
            correct:         Correctness of submission (Boolean)
            score:           Points to be assigned (numeric, can be float)
            msg:             Message from grader to display to student (string)
        """
        fail = (False, False, 0, '')
        try:
            score_result = json.loads(score_msg)
        except (TypeError, ValueError):
            log.error("External grader message should be a JSON-serialized dict."
                      " Received score_msg = %s", score_msg)
            return fail
        if not isinstance(score_result, dict):
            log.error("External grader message should be a JSON-serialized dict."
                      " Received score_result = %s", score_result)
            return fail
        for tag in ['correct', 'score', 'msg']:
            if tag not in score_result:
                log.error("External grader message is missing one or more required"
                          " tags: 'correct', 'score', 'msg'")
                return fail

        # Next, we need to check that the contents of the external grader message is safe for the LMS.
        # 1) Make sure that the message is valid XML (proper opening/closing tags)
        # 2) If it is not valid XML, make sure it is valid HTML. Note: html5lib parser will try to
        # repair any broken HTML
        # For example: <aaa></bbb> will become <aaa/>.
        msg = score_result['msg']

        try:
            etree.fromstring(msg)
        except etree.XMLSyntaxError as _err:
            # If `html` contains attrs with no values, like `controls` in <audio controls src='smth'/>,
            # XML parser will raise exception, so wee fallback to html5parser, which will set empty ""
            # values for such attrs.
            parsed = html5lib.parseFragment(msg, treebuilder='lxml', namespaceHTMLElements=False)
            if not parsed:
                log.error("Unable to parse external grader message as valid"
                      " XML: score_msg['msg']=%s", msg)
                return fail

        return (True, score_result['correct'], score_result['score'], msg)


#-----------------------------------------------------------------------------


@registry.register
class ExternalResponse(LoncapaResponse):
    """
    Grade the students input using an external server.

    Typically used by coding problems.

    """

    tags = ['externalresponse']
    allowed_inputfields = ['textline', 'textbox']
    awdmap = {
        'EXACT_ANS': 'correct',         # TODO: handle other loncapa responses
        'WRONG_FORMAT': 'incorrect',
    }

    def __init__(self, *args, **kwargs):
        self.url = ''
        self.tests = []
        self.code = ''
        super(ExternalResponse, self).__init__(*args, **kwargs)

    def setup_response(self):
        super(ExternalResponse, self).setup_response()

        xml = self.xml
        # FIXME - hardcoded URL
        self.url = xml.get('url') or "http://qisx.mit.edu:8889/pyloncapa"

        answer = xml.find('answer')
        if answer is not None:
            answer_src = answer.get('src')
            if answer_src is not None:
                # TODO: this code seems not to be used any more since self.capa_system.filesystem doesn't exist.
                self.code = self.capa_system.filesystem.open('src/' + answer_src).read()
            else:
                self.code = answer.text
        else:
            # no <answer> stanza; get code from <script>
            self.code = self.context['script_code']
            if not self.code:
                msg = '%s: Missing answer script code for externalresponse' % unicode(
                    self)
                msg += "\nSee XML source line %s" % getattr(
                    self.xml, 'sourceline', '<unavailable>')
                raise LoncapaProblemError(msg)

        self.tests = xml.get('tests')

    def do_external_request(self, cmd, extra_payload):
        """
        Perform HTTP request / post to external server.

        cmd = remote command to perform (str)
        extra_payload = dict of extra stuff to post.

        Return XML tree of response (from response body)
        """
        xmlstr = etree.tostring(self.xml, pretty_print=True)
        payload = {
            'xml': xmlstr,
            'edX_cmd': cmd,
            'edX_tests': self.tests,
            'processor': self.code,
        }
        payload.update(extra_payload)

        try:
            # call external server. TODO: synchronous call, can block for a
            # long time
            req = requests.post(self.url, data=payload)
        except Exception as err:
            msg = 'Error {0} - cannot connect to external server url={1}'.format(err, self.url)
            log.error(msg)
            raise Exception(msg)

        if self.capa_system.DEBUG:
            log.info('response = %s', req.text)

        if (not req.text) or (not req.text.strip()):
            raise Exception(
                'Error: no response from external server url=%s' % self.url)

        try:
            # response is XML; parse it
            rxml = etree.fromstring(req.text)
        except Exception as err:
            msg = 'Error {0} - cannot parse response from external server req.text={1}'.format(err, req.text)
            log.error(msg)
            raise Exception(msg)

        return rxml

    def get_score(self, student_answers):
        idset = sorted(self.answer_ids)
        cmap = CorrectMap()
        try:
            submission = [student_answers[k] for k in idset]
        except Exception as err:  # pylint: disable=W0703
            log.error(
                'Error %s: cannot get student answer for %s; student_answers=%s',
                err,
                self.answer_ids,
                student_answers
            )
            raise Exception(err)

        self.context.update({'submission': submission})

        extra_payload = {'edX_student_response': json.dumps(submission)}

        try:
            rxml = self.do_external_request('get_score', extra_payload)
        except Exception as err:  # pylint: disable=W0703
            log.error('Error %s', err)
            if self.capa_system.DEBUG:
                cmap.set_dict(dict(zip(sorted(
                    self.answer_ids), ['incorrect'] * len(idset))))
                cmap.set_property(
                    self.answer_ids[0], 'msg',
                    '<span class="inline-error">%s</span>' % str(err).replace('<', '&lt;'))
                return cmap

        awd = rxml.find('awarddetail').text

        self.context['correct'] = ['correct']
        if awd in self.awdmap:
            self.context['correct'][0] = self.awdmap[awd]

        # create CorrectMap
        for key in idset:
            idx = idset.index(key)
            msg = rxml.find('message').text.replace(
                '&nbsp;', '&#160;') if idx == 0 else None
            cmap.set(key, self.context['correct'][idx], msg=msg)

        return cmap

    def get_answers(self):
        """
        Use external server to get expected answers
        """
        try:
            rxml = self.do_external_request('get_answers', {})
            exans = json.loads(rxml.find('expected').text)
        except Exception as err:  # pylint: disable=W0703
            log.error('Error %s', err)
            if self.capa_system.DEBUG:
                msg = '<span class="inline-error">%s</span>' % str(
                    err).replace('<', '&lt;')
                exans = [''] * len(self.answer_ids)
                exans[0] = msg

        if not (len(exans) == len(self.answer_ids)):
            log.error('Expected %s answers from external server, only got %s!',
                      len(self.answer_ids), len(exans))
            raise Exception('Short response from external server')
        return dict(zip(self.answer_ids, exans))


#-----------------------------------------------------------------------------

@registry.register
class FormulaResponse(LoncapaResponse):
    """
    Checking of symbolic math response using numerical sampling.
    """

    tags = ['formularesponse']
    hint_tag = 'formulahint'
    allowed_inputfields = ['textline', 'formulaequationinput']
    required_attributes = ['answer', 'samples']
    max_inputfields = 1

    def __init__(self, *args, **kwargs):
        self.correct_answer = ''
        self.samples = ''
        self.tolerance = default_tolerance
        self.case_sensitive = False
        super(FormulaResponse, self).__init__(*args, **kwargs)

    def setup_response(self):
        super(FormulaResponse, self).setup_response()

        xml = self.xml
        context = self.context
        self.correct_answer = contextualize_text(xml.get('answer'), context)
        self.samples = contextualize_text(xml.get('samples'), context)

        # Find the tolerance
        tolerance_xml = xml.xpath(
            '//*[@id=$id]//responseparam[@type="tolerance"]/@default',
            id=xml.get('id')
        )
        if tolerance_xml:  # If it isn't an empty list...
            self.tolerance = contextualize_text(tolerance_xml[0], context)

        types = xml.get('type')
        if types is None:
            typeslist = []
        else:
            typeslist = types.split(',')
        if 'ci' in typeslist:
            # Case insensitive
            self.case_sensitive = False
        elif 'cs' in typeslist:
            # Case sensitive
            self.case_sensitive = True
        else:
            # Default
            self.case_sensitive = False

    def get_score(self, student_answers):
        given = student_answers[self.answer_id]
        correctness = self.check_formula(
            self.correct_answer,
            given,
            self.samples
        )
        return CorrectMap(self.answer_id, correctness)

    def tupleize_answers(self, answer, var_dict_list):
        """
        Takes in an answer and a list of dictionaries mapping variables to values.
        Each dictionary represents a test case for the answer.
        Returns a tuple of formula evaluation results.
        """
        _ = self.capa_system.i18n.ugettext

        out = []
        for var_dict in var_dict_list:
            try:
                out.append(evaluator(
                    var_dict,
                    dict(),
                    answer,
                    case_sensitive=self.case_sensitive,
                ))
            except UndefinedVariable as err:
                log.debug(
                    'formularesponse: undefined variable in formula=%s',
                    cgi.escape(answer)
                )
                raise StudentInputError(
                    _("Invalid input: {bad_input} not permitted in answer.").format(bad_input=err.message)
                )
            except ValueError as err:
                if 'factorial' in err.message:
                    # This is thrown when fact() or factorial() is used in a formularesponse answer
                    #   that tests on negative and/or non-integer inputs
                    # err.message will be: `factorial() only accepts integral values` or
                    # `factorial() not defined for negative values`
                    log.debug(
                        ('formularesponse: factorial function used in response '
                         'that tests negative and/or non-integer inputs. '
                         'Provided answer was: %s'),
                        cgi.escape(answer)
                    )
                    raise StudentInputError(
                        _("factorial function not permitted in answer "
                          "for this problem. Provided answer was: "
                          "{bad_input}").format(bad_input=cgi.escape(answer))
                    )
                # If non-factorial related ValueError thrown, handle it the same as any other Exception
                log.debug('formularesponse: error %s in formula', err)
                raise StudentInputError(
                    _("Invalid input: Could not parse '{bad_input}' as a formula.").format(
                        bad_input=cgi.escape(answer)
                    )
                )
            except Exception as err:
                # traceback.print_exc()
                log.debug('formularesponse: error %s in formula', err)
                raise StudentInputError(
                    _("Invalid input: Could not parse '{bad_input}' as a formula").format(
                        bad_input=cgi.escape(answer)
                    )
                )
        return out

    def randomize_variables(self, samples):
        """
        Returns a list of dictionaries mapping variables to random values in range,
        as expected by tupleize_answers.
        """
        variables = samples.split('@')[0].split(',')
        numsamples = int(samples.split('@')[1].split('#')[1])
        sranges = zip(*map(lambda x: map(float, x.split(",")),
                           samples.split('@')[1].split('#')[0].split(':')))
        ranges = dict(zip(variables, sranges))

        out = []
        for _ in range(numsamples):
            var_dict = {}
            # ranges give numerical ranges for testing
            for var in ranges:
                # TODO: allow specified ranges (i.e. integers and complex numbers) for random variables
                value = random.uniform(*ranges[var])
                var_dict[str(var)] = value
            out.append(var_dict)
        return out

    def check_formula(self, expected, given, samples):
        """
        Given an expected answer string, a given (student-produced) answer
        string, and a samples string, return whether the given answer is
        "correct" or "incorrect".
        """
        var_dict_list = self.randomize_variables(samples)
        student_result = self.tupleize_answers(given, var_dict_list)
        instructor_result = self.tupleize_answers(expected, var_dict_list)

        correct = all(compare_with_tolerance(student, instructor, self.tolerance)
                      for student, instructor in zip(student_result, instructor_result))
        if correct:
            return "correct"
        else:
            return "incorrect"

    def compare_answer(self, ans1, ans2):
        """
        An external interface for comparing whether a and b are equal.
        """
        internal_result = self.check_formula(ans1, ans2, self.samples)
        return internal_result == "correct"

    def validate_answer(self, answer):
        """
        Returns whether this answer is in a valid form.
        """
        var_dict_list = self.randomize_variables(self.samples)
        try:
            self.tupleize_answers(answer, var_dict_list)
            return True
        except StudentInputError:
            return False

    def strip_dict(self, inp_d):
        """
        Takes a dict. Returns an identical dict, with all non-word
        keys and all non-numeric values stripped out. All values also
        converted to float. Used so we can safely use Python contexts.
        """
        inp_d = dict([(k, numpy.complex(inp_d[k]))
                      for k in inp_d if type(k) == str and
                      k.isalnum() and
                      isinstance(inp_d[k], numbers.Number)])
        return inp_d

    def check_hint_condition(self, hxml_set, student_answers):
        given = student_answers[self.answer_id]
        hints_to_show = []
        for hxml in hxml_set:
            samples = hxml.get('samples')
            name = hxml.get('name')
            correct_answer = contextualize_text(
                hxml.get('answer'), self.context)
            # pylint: disable=W0703
            try:
                correctness = self.check_formula(
                    correct_answer,
                    given,
                    samples
                )
            except Exception:
                correctness = 'incorrect'
            if correctness == 'correct':
                hints_to_show.append(name)
        log.debug('hints_to_show = %s', hints_to_show)
        return hints_to_show

    def get_answers(self):
        return {self.answer_id: self.correct_answer}

#-----------------------------------------------------------------------------


@registry.register
class SchematicResponse(LoncapaResponse):
    """
    Circuit schematic response type.
    """
    tags = ['schematicresponse']
    allowed_inputfields = ['schematic']

    def __init__(self, *args, **kwargs):
        self.code = ''
        super(SchematicResponse, self).__init__(*args, **kwargs)

    def setup_response(self):
        super(SchematicResponse, self).setup_response()

        xml = self.xml
        answer = xml.xpath('//*[@id=$id]//answer', id=xml.get('id'))[0]
        answer_src = answer.get('src')
        if answer_src is not None:
            # Untested; never used
            self.code = self.capa_system.filestore.open('src/' + answer_src).read()
        else:
            self.code = answer.text

    def get_score(self, student_answers):
        #from capa_problem import global_context
        submission = [
            json.loads(student_answers[k]) for k in sorted(self.answer_ids)
        ]
        self.context.update({'submission': submission})
        try:
            safe_exec.safe_exec(
                self.code,
                self.context,
                cache=self.capa_system.cache,
                slug=self.id,
                random_seed=self.context['seed'],
                unsafely=self.capa_system.can_execute_unsafe_code(),
            )
        except Exception as err:
            _ = self.capa_system.i18n.ugettext
            # Translators: 'SchematicResponse' is a problem type and should not be translated.
            msg = _('Error in evaluating SchematicResponse. The error was: {error_msg}').format(error_msg=err)
            raise ResponseError(msg)
        cmap = CorrectMap()
        cmap.set_dict(dict(zip(sorted(self.answer_ids), self.context['correct'])))
        return cmap

    def get_answers(self):
        # use answers provided in input elements
        return self.default_answer_map

#-----------------------------------------------------------------------------


@registry.register
class ImageResponse(LoncapaResponse):
    """
    Handle student response for image input: the input is a click on an image,
    which produces an [x,y] coordinate pair.  The click is correct if it falls
    within a region specified.  This region is a union of rectangles.

    Lon-CAPA requires that each <imageresponse> has a <foilgroup> inside it.
    That doesn't make sense to me (Ike).  Instead, let's have it such that
    <imageresponse> should contain one or more <imageinput> stanzas.
    Each <imageinput> should specify a rectangle(s) or region(s), given as an
    attribute, defining the correct answer.

    <imageinput src="/static/images/Lecture2/S2_p04.png" width="811" height="610"
    rectangle="(10,10)-(20,30);(12,12)-(40,60)"
    regions="[[[10,10], [20,30], [40, 10]], [[100,100], [120,130], [110,150]]]"/>

    Regions is list of lists [region1, region2, region3, ...] where regionN
    is disordered list of points: [[1,1], [100,100], [50,50], [20, 70]].

    If there is only one region in the list, simpler notation can be used:
    regions="[[10,10], [30,30], [10, 30], [30, 10]]" (without explicitly
        setting outer list)

    Returns:
        True, if click is inside any region or rectangle. Otherwise False.
    """

    tags = ['imageresponse']
    allowed_inputfields = ['imageinput']

    def __init__(self, *args, **kwargs):
        self.ielements = []
        super(ImageResponse, self).__init__(*args, **kwargs)

    def setup_response(self):
        super(ImageResponse, self).setup_response()

        self.ielements = self.inputfields
        self.answer_ids = [ie.get('id') for ie in self.ielements]

    def get_score(self, student_answers):
        _ = self.capa_system.i18n.ugettext
        correct_map = CorrectMap()
        expectedset = self.get_mapped_answers()
        for aid in self.answer_ids:  # loop through IDs of <imageinput>
            # Fields in our stanza
            given = student_answers[aid]  # This should be a string of the form '[x,y]'
            correct_map.set(aid, 'incorrect')
            if not given:  # No answer to parse. Mark as incorrect and move on
                continue
            # Parse given answer
            acoords = re.match(r'\[([0-9]+),([0-9]+)]', given.strip().replace(' ', ''))
            if not acoords:
                msg = _('error grading {image_input_id} (input={user_input})').format(
                    image_input_id=aid,
                    user_input=given
                )
                raise Exception('[capamodule.capa.responsetypes.imageinput] ' + msg)

            (ans_x, ans_y) = [int(x) for x in acoords.groups()]

            rectangles, regions = expectedset
            if rectangles[aid]:  # Rectangles part - for backward compatibility
                # Check whether given point lies in any of the solution
                # rectangles
                solution_rectangles = rectangles[aid].split(';')
                for solution_rectangle in solution_rectangles:
                    # parse expected answer
                    # TODO: Compile regexp on file load
                    sr_coords = re.match(
                        r'[\(\[]([0-9]+),([0-9]+)[\)\]]-[\(\[]([0-9]+),([0-9]+)[\)\]]',
                        solution_rectangle.strip().replace(' ', ''))
                    if not sr_coords:
                        # Translators: {sr_coords} are the coordinates of a rectangle
                        msg = _('Error in problem specification! Cannot parse rectangle in {sr_coords}').format(
                            sr_coords=etree.tostring(self.ielements[aid], pretty_print=True)
                        )
                        raise Exception('[capamodule.capa.responsetypes.imageinput] ' + msg)

                    (llx, lly, urx, ury) = [int(x) for x in sr_coords.groups()]

                    # answer is correct if (x,y) is within the specified
                    # rectangle
                    if (llx <= ans_x <= urx) and (lly <= ans_y <= ury):
                        correct_map.set(aid, 'correct')
                        break
            if correct_map[aid]['correctness'] != 'correct' and regions[aid]:
                parsed_region = json.loads(regions[aid])
                if parsed_region:
                    if type(parsed_region[0][0]) != list:
                        # we have [[1,2],[3,4],[5,6]] - single region
                        # instead of [[[1,2],[3,4],[5,6], [[1,2],[3,4],[5,6]]]
                        # or [[[1,2],[3,4],[5,6]]] - multiple regions syntax
                        parsed_region = [parsed_region]
                    for region in parsed_region:
                        polygon = MultiPoint(region).convex_hull
                        if (polygon.type == 'Polygon' and
                                polygon.contains(Point(ans_x, ans_y))):
                            correct_map.set(aid, 'correct')
                            break
        return correct_map

    def get_mapped_answers(self):
        """
        Returns the internal representation of the answers

        Input:
            None
        Returns:
            tuple (dict, dict) -
                rectangles (dict) - a map of inputs to the defined rectangle for that input
                regions (dict) - a map of inputs to the defined region for that input
        """
        answers = (
            dict([(ie.get('id'), ie.get(
                'rectangle')) for ie in self.ielements]),
            dict([(ie.get('id'), ie.get('regions')) for ie in self.ielements]))
        return answers

    def get_answers(self):
        """
        Returns the external representation of the answers

        Input:
            None
        Returns:
            dict (str, (str, str)) - a map of inputs to a tuple of their rectangle
                and their regions
        """
        answers = {}
        for ielt in self.ielements:
            ie_id = ielt.get('id')
            answers[ie_id] = {'rectangle': ielt.get('rectangle'), 'regions': ielt.get('regions')}

        return answers

#-----------------------------------------------------------------------------


@registry.register
class AnnotationResponse(LoncapaResponse):
    """
    Checking of annotation responses.

    The response contains both a comment (student commentary) and an option (student tag).
    Only the tag is currently graded. Answers may be incorrect, partially correct, or correct.
    """
    tags = ['annotationresponse']
    allowed_inputfields = ['annotationinput']
    max_inputfields = 1
    default_scoring = {'incorrect': 0, 'partially-correct': 1, 'correct': 2}

    def __init__(self, *args, **kwargs):
        self.scoring_map = {}
        self.answer_map = {}
        super(AnnotationResponse, self).__init__(*args, **kwargs)

    def setup_response(self):
        super(AnnotationResponse, self).setup_response()

        self.scoring_map = self._get_scoring_map()
        self.answer_map = self._get_answer_map()
        self.maxpoints = self._get_max_points()

    def get_score(self, student_answers):
        """
        Returns a CorrectMap for the student answer, which may include
        partially correct answers.
        """
        student_answer = student_answers[self.answer_id]
        student_option = self._get_submitted_option_id(student_answer)

        scoring = self.scoring_map[self.answer_id]
        is_valid = student_option is not None and student_option in scoring.keys(
        )

        (correctness, points) = ('incorrect', None)
        if is_valid:
            correctness = scoring[student_option]['correctness']
            points = scoring[student_option]['points']

        return CorrectMap(self.answer_id, correctness=correctness, npoints=points)

    def get_answers(self):
        return self.answer_map

    def _get_scoring_map(self):
        """Returns a dict of option->scoring for each input."""
        scoring = self.default_scoring
        choices = dict([(choice, choice) for choice in scoring])
        scoring_map = {}

        for inputfield in self.inputfields:
            option_scoring = dict([(
                option['id'],
                {
                    'correctness': choices.get(option['choice']),
                    'points': scoring.get(option['choice'])
                }
            ) for option in self._find_options(inputfield)])

            scoring_map[inputfield.get('id')] = option_scoring

        return scoring_map

    def _get_answer_map(self):
        """Returns a dict of answers for each input."""
        answer_map = {}
        for inputfield in self.inputfields:
            correct_option = self._find_option_with_choice(
                inputfield, 'correct')
            if correct_option is not None:
                input_id = inputfield.get('id')
                answer_map[input_id] = correct_option.get('description')
        return answer_map

    def _get_max_points(self):
        """Returns a dict of the max points for each input: input id -> maxpoints."""
        scoring = self.default_scoring
        correct_points = scoring.get('correct')
        return dict([(inputfield.get('id'), correct_points) for inputfield in self.inputfields])

    def _find_options(self, inputfield):
        """Returns an array of dicts where each dict represents an option. """
        elements = inputfield.findall('./options/option')
        return [{
                'id': index,
                'description': option.text,
                'choice': option.get('choice')
                } for (index, option) in enumerate(elements)]

    def _find_option_with_choice(self, inputfield, choice):
        """Returns the option with the given choice value, otherwise None. """
        for option in self._find_options(inputfield):
            if option['choice'] == choice:
                return option

    def _unpack(self, json_value):
        """Unpacks a student response value submitted as JSON."""
        json_d = json.loads(json_value)
        if type(json_d) != dict:
            json_d = {}

        comment_value = json_d.get('comment', '')
        if not isinstance(json_d, basestring):
            comment_value = ''

        options_value = json_d.get('options', [])
        if not isinstance(options_value, list):
            options_value = []

        return {
            'options_value': options_value,
            'comment_value': comment_value
        }

    def _get_submitted_option_id(self, student_answer):
        """Return the single option that was selected, otherwise None."""
        submitted = self._unpack(student_answer)
        option_ids = submitted['options_value']
        if len(option_ids) == 1:
            return option_ids[0]
        return None


@registry.register
class ChoiceTextResponse(LoncapaResponse):
    """
    Allows for multiple choice responses with text inputs
    Desired semantics match those of NumericalResponse and
    ChoiceResponse.
    """

    tags = ['choicetextresponse']
    max_inputfields = 1
    allowed_inputfields = ['choicetextgroup',
                           'checkboxtextgroup',
                           'radiotextgroup'
                           ]

    def __init__(self, *args, **kwargs):
        self.correct_inputs = {}
        self.answer_values = {}
        self.correct_choices = {}
        super(ChoiceTextResponse, self).__init__(*args, **kwargs)

    def setup_response(self):
        """
        Sets up three dictionaries for use later:
        `correct_choices`: These are the correct binary choices(radio/checkbox)
        `correct_inputs`: These are the numerical/string answers for required
        inputs.
        `answer_values`: This is a dict, keyed by the name of the binary choice
            which contains the correct answers for the text inputs separated by
            commas e.g. "1, 0.5"

        `correct_choices` and `correct_inputs` are used for grading the problem
        and `answer_values` is used for displaying correct answers.

        """
        super(ChoiceTextResponse, self).setup_response()

        _ = self.capa_system.i18n.ugettext
        context = self.context
        self.answer_values = {self.answer_id: []}
        self.assign_choice_names()
        correct_xml = self.xml.xpath('//*[@id=$id]//choice[@correct="true"]',
                                     id=self.xml.get('id'))

        for node in correct_xml:
            # For each correct choice, set the `parent_name` to the
            # current choice's name
            parent_name = node.get('name')
            # Add the name of the correct binary choice to the
            # correct choices list as a key. The value is not important.
            self.correct_choices[parent_name] = {'answer': ''}
            # Add the name of the parent to the list of correct answers
            self.answer_values[self.answer_id].append(parent_name)
            answer_list = []
            # Loop over <numtolerance_input> elements inside of the correct choices
            for child in node:
                answer = child.get('answer', None)
                if not answer:
                    # If the question creator does not specify an answer for a
                    # <numtolerance_input> inside of a correct choice, raise an error
                    raise LoncapaProblemError(
                        _("Answer not provided for {input_type}").format(input_type="numtolerance_input")
                    )
                # Contextualize the answer to allow script generated answers.
                answer = contextualize_text(answer, context)
                input_name = child.get('name')
                # Contextualize the tolerance to value.
                tolerance = contextualize_text(
                    child.get('tolerance', default_tolerance),
                    context
                )
                # Add the answer and tolerance information for the current
                # numtolerance_input to `correct_inputs`
                self.correct_inputs[input_name] = {
                    'answer': answer,
                    'tolerance': tolerance
                }
                # Add the correct answer for this input to the list for show
                answer_list.append(answer)
            # Turn the list of numtolerance_input answers into a comma separated string.
            self.answer_values[parent_name] = ', '.join(answer_list)
        # Turn correct choices into a set. Allows faster grading.
        self.correct_choices = set(self.correct_choices.keys())

    def assign_choice_names(self):
        """
        Initialize name attributes in <choice> and <numtolerance_input> tags
        for this response.

        Example:
        Assuming for simplicity that `self.answer_id` = '1_2_1'

        Before the function is called `self.xml` =
        <radiotextgroup>
            <choice correct = "true">
                The number
                    <numtolerance_input answer="5"/>
                Is the mean of the list.
            </choice>
            <choice correct = "false">
                False demonstration choice
            </choice>
        </radiotextgroup>

        After this is called the choices and numtolerance_inputs will have a name
        attribute initialized and self.xml will be:

        <radiotextgroup>
        <choice correct = "true" name ="1_2_1_choiceinput_0bc">
            The number
                <numtolerance_input name = "1_2_1_choiceinput0_numtolerance_input_0"
                 answer="5"/>
            Is the mean of the list.
        </choice>
        <choice correct = "false" name = "1_2_1_choiceinput_1bc>
            False demonstration choice
        </choice>
        </radiotextgroup>
        """

        for index, choice in enumerate(
            self.xml.xpath('//*[@id=$id]//choice', id=self.xml.get('id'))
        ):
            # Set the name attribute for <choices>
            # "bc" is appended at the end to indicate that this is a
            # binary choice as opposed to a numtolerance_input, this convention
            # is used when grading the problem
            choice.set(
                "name",
                self.answer_id + "_choiceinput_" + str(index) + "bc"
            )
            # Set Name attributes for <numtolerance_input> elements
            # Look for all <numtolerance_inputs> inside this choice.
            numtolerance_inputs = choice.findall('numtolerance_input')
            # Look for all <decoy_input> inside this choice
            decoys = choice.findall('decoy_input')
            # <decoy_input> would only be used in choices which do not contain
            # <numtolerance_input>
            inputs = numtolerance_inputs if numtolerance_inputs else decoys
            # Give each input inside of the choice a name combining
            # The ordinality of the choice, and the ordinality of the input
            # within that choice e.g. 1_2_1_choiceinput_0_numtolerance_input_1
            for ind, child in enumerate(inputs):
                child.set(
                    "name",
                    self.answer_id + "_choiceinput_" + str(index) +
                    "_numtolerance_input_" + str(ind)
                )

    def get_score(self, student_answers):
        """
        Returns a `CorrectMap` showing whether `student_answers` are correct.

        `student_answers` contains keys for binary inputs(radiobutton,
        checkbox) and numerical inputs. Keys ending with 'bc' are binary
        choice inputs otherwise they are text fields.

        This method first separates the two
        types of answers and then grades them in separate methods.

        The student is only correct if they have both the binary inputs and
        numerical inputs correct.
        """
        answer_dict = student_answers.get(self.answer_id, "")
        binary_choices, numtolerance_inputs = self._split_answers_dict(answer_dict)
        # Check the binary choices first.
        choices_correct = self._check_student_choices(binary_choices)
        inputs_correct = self._check_student_inputs(numtolerance_inputs)
        # Only return correct if the student got both the binary
        # and numtolerance_inputs are correct
        correct = choices_correct and inputs_correct

        return CorrectMap(
            self.answer_id,
            'correct' if correct else 'incorrect'
        )

    def get_answers(self):
        """
        Returns a dictionary containing the names of binary choices as keys
        and a string of answers to any numtolerance_inputs which they may have
        e.g {choice_1bc : "answer1, answer2", choice_2bc : ""}
        """
        return self.answer_values

    def _split_answers_dict(self, a_dict):
        """
        Returns two dicts:
        `binary_choices` : dictionary {input_name: input_value} for
        the binary choices which the student selected.
        and
        `numtolerance_choices` : a dictionary {input_name: input_value}
        for the numtolerance_inputs inside of choices which were selected

        Determines if an input is inside of a binary input by looking at
        the beginning of it's name.

        For example. If a binary_choice was named '1_2_1_choiceinput_0bc'
        All of the numtolerance_inputs in it would have an idea that begins
        with '1_2_1_choice_input_0_numtolerance_input'

        Splits the name of the numtolerance_input at the occurence of
        '_numtolerance_input_' and appends 'bc' to the end to get the name
        of the choice it is contained in.

        Example:
        `a_dict` = {
            '1_2_1_choiceinput_0bc': '1_2_1_choiceinput_0bc',
            '1_2_1_choiceinput_0_numtolerance_input_0': '1',
            '1_2_1_choiceinput_0_numtolerance_input_1': '2'
            '1_2_1_choiceinput_1_numtolerance_input_0': '3'
        }

        In this case, the binary choice is '1_2_1_choiceinput_0bc', and
        the numtolerance_inputs associated with it are
        '1_2_1_choiceinput_0_numtolerance_input_0', and
        '1_2_1_choiceinput_0_numtolerance_input_1'.

        so the two return dictionaries would be
        `binary_choices` = {'1_2_1_choiceinput_0bc': '1_2_1_choiceinput_0bc'}
        and
        `numtolerance_choices` ={
            '1_2_1_choiceinput_0_numtolerance_input_0': '1',
            '1_2_1_choiceinput_0_numtolerance_input_1': '2'
        }

        The entry '1_2_1_choiceinput_1_numtolerance_input_0': '3' is discarded
        because it was not inside of a selected binary choice, and no validation
        should be performed on numtolerance_inputs inside of non-selected choices.
        """

        # Initialize the two dictionaries that are returned
        numtolerance_choices = {}
        binary_choices = {}

        # `selected_choices` is a list of binary choices which were "checked/selected"
        # when the student submitted the problem.
        # Keys in a_dict ending with 'bc' refer to binary choices.
        selected_choices = [key for key in a_dict if key.endswith("bc")]
        for key in selected_choices:
            binary_choices[key] = a_dict[key]

        # Convert the name of a numtolerance_input into the name of the binary
        # choice that it is contained within, and append it to the list if
        # the numtolerance_input's parent binary_choice is contained in
        # `selected_choices`.
        selected_numtolerance_inputs = [
            key for key in a_dict if key.partition("_numtolerance_input_")[0] + "bc"
            in selected_choices
        ]

        for key in selected_numtolerance_inputs:
            numtolerance_choices[key] = a_dict[key]

        return (binary_choices, numtolerance_choices)

    def _check_student_choices(self, choices):
        """
        Compares student submitted checkbox/radiobutton answers against
        the correct answers. Returns True or False.

        True if all of the correct choices are selected and no incorrect
        choices are selected.
        """
        student_choices = set(choices)
        required_selected = len(self.correct_choices - student_choices) == 0
        no_extra_selected = len(student_choices - self.correct_choices) == 0
        correct = required_selected and no_extra_selected
        return correct

    def _check_student_inputs(self, numtolerance_inputs):
        """
        Compares student submitted numerical answers against the correct
        answers and tolerances.

        `numtolerance_inputs` is a dictionary {answer_name : answer_value}

        Performs numerical validation by means of calling
        `compare_with_tolerance()` on all of `numtolerance_inputs`

        Performs a call to `compare_with_tolerance` even on values for
        decoy_inputs. This is used to validate their numericality and
        raise an error if the student entered a non numerical expression.

        Returns True if and only if all student inputs are correct.
        """
        _ = self.capa_system.i18n.ugettext
        inputs_correct = True
        for answer_name, answer_value in numtolerance_inputs.iteritems():
            # If `self.corrrect_inputs` does not contain an entry for
            # `answer_name`, this means that answer_name is a decoy
            # input's value, and validation of its numericality is the
            # only thing of interest from the later call to
            # `compare_with_tolerance`.
            params = self.correct_inputs.get(answer_name, {'answer': 0})

            correct_ans = params['answer']
            # Set the tolerance to '0' if it was not specified in the xml
            tolerance = params.get('tolerance', default_tolerance)
            # Make sure that the staff answer is a valid number
            try:
                correct_ans = complex(correct_ans)
            except ValueError:
                log.debug(
                    "Content error--answer '%s' is not a valid complex number",
                    correct_ans
                )
                raise StudentInputError(
                    _("The Staff answer could not be interpreted as a number.")
                )
            # Compare the student answer to the staff answer/ or to 0
            # if all that is important is verifying numericality
            try:
                partial_correct = compare_with_tolerance(
                    evaluator({}, {}, answer_value),
                    correct_ans,
                    tolerance
                )
            except:
                # Use the traceback-preserving version of re-raising with a
                # different type
                __, __, trace = sys.exc_info()
                msg = _("Could not interpret '{given_answer}' as a number.").format(
                    given_answer=cgi.escape(answer_value)
                )
                msg += " ({0})".format(trace)
                raise StudentInputError(msg)

            # Ignore the results of the comparisons which were just for
            # Numerical Validation.
            if answer_name in self.correct_inputs and not partial_correct:
                # If any input is not correct, set the return value to False
                inputs_correct = False
        return inputs_correct

#-----------------------------------------------------------------------------

# TEMPORARY: List of all response subclasses
# FIXME: To be replaced by auto-registration

# pylint: disable=E0604
__all__ = [CodeResponse,
           NumericalResponse,
           FormulaResponse,
           CustomResponse,
           SchematicResponse,
           ExternalResponse,
           ImageResponse,
           OptionResponse,
           SymbolicResponse,
           StringResponse,
           ChoiceResponse,
           MultipleChoiceResponse,
           TrueFalseResponse,
           JavascriptResponse,
           AnnotationResponse,
           ChoiceTextResponse]
