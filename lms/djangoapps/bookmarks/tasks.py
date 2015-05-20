"""
Tasks for bookmarks.
"""
import logging

from django.db import transaction

from celery.task import task  # pylint: disable=import-error,no-name-in-module
from opaque_keys.edx.keys import CourseKey
from xmodule.modulestore.django import modulestore

from .models import XBlockCache, PathItem

log = logging.getLogger('edx.celery.task')


def _calculate_course_xblocks_data(course_key):
    """
    Fetch data for all the blocks in the course.

    This data consists of the display_name and path of the block.
    """
    course = modulestore().get_course(course_key, depth=None)

    blocks_info_dict = {}

    # Collect display_name and children usage keys.
    blocks_stack = [course]
    while blocks_stack:
        current_block = blocks_stack.pop()
        children = current_block.get_children() if current_block.has_children else []
        usage_id = unicode(current_block.scope_ids.usage_id)
        block_info = {
            'usage_key': current_block.scope_ids.usage_id,
            'display_name': current_block.display_name,
            'children_ids': [unicode(child.scope_ids.usage_id) for child in children]
        }
        blocks_info_dict[usage_id] = block_info

        # Add this blocks children to the stack so that we can traverse them as well.
        blocks_stack.extend(children)

    # Set children
    for block in blocks_info_dict.values():
        block.setdefault('children', [])
        for child_id in block['children_ids']:
            block['children'].append(blocks_info_dict[child_id])
        block.pop('children_ids', None)

    # Calculate paths
    def add_path_info(block_info, current_path):
        """Do a DFS and add paths info to each block_info."""

        block_info.setdefault('paths', [])
        block_info['paths'].append(current_path)

        for child_block_info in block_info['children']:
            add_path_info(child_block_info, current_path + [block_info])

    add_path_info(blocks_info_dict[unicode(course.scope_ids.usage_id)], [])

    return blocks_info_dict


def _paths_from_data(paths_data):
    """
    Construct a list of paths from path data.
    """
    paths = []
    for path_data in paths_data:
        paths.append(
            [PathItem(item['usage_key'], item['display_name']) for item in path_data]
        )
    return paths


def _update_xblocks_cache(course_key):
    """
    Calculate the XBlock cache data for a course and update the XBlockCache table.
    """
    blocks_data = _calculate_course_xblocks_data(course_key)

    with transaction.commit_on_success():
        block_caches = XBlockCache.objects.filter(course_key=course_key)
        for block_cache in block_caches:
            block_data = blocks_data.pop(unicode(block_cache.usage_key))
            paths = _paths_from_data(block_data['paths'])
            if block_cache.display_name != block_data['display_name'] or block_cache.paths != paths:
                block_cache.display_name = block_data['display_name']
                block_cache.paths = paths
                block_cache.save()

    for block_data in blocks_data.values():
        with transaction.commit_on_success():
            paths = _paths_from_data(block_data['paths'])
            block_cache, created = XBlockCache.objects.get_or_create(usage_key=block_data['usage_key'], defaults={
                'course_key': course_key,
                'display_name': block_data['display_name'],
                'paths': paths,
            })

            if not created:
                if block_cache.display_name != block_data['display_name'] or block_cache.paths != block_data['paths']:
                    block_cache.display_name = block_data['display_name']
                    block_cache.paths = block_data['paths']
                    block_cache.save()


@task(name=u'lms.djangoapps.bookmarks.tasks.update_xblock_cache')
def update_xblocks_cache(course_id):
    """
    Update the XBlocks cache for a course.

    Arguments:
        course_id (String): The course_id of a course.
    """
    # Ideally we'd like to accept a CourseLocator; however, CourseLocator is not JSON-serializable (by default) so
    # Celery's delayed tasks fail to start. For this reason, callers should pass the course key as a Unicode string.
    if not isinstance(course_id, basestring):
        raise ValueError('course_id must be a string. {} is not acceptable.'.format(type(course_id)))

    course_key = CourseKey.from_string(course_id)
    _update_xblocks_cache(course_key)
