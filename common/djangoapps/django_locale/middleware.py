# TODO: This file is imported from the stable Django 1.8 branch. Remove this file
# and re-import this middleware from Django once the codebase is upgraded.
# pylint: disable=invalid-name, missing-docstring
"This is the locale selecting middleware that will look at accept headers"

from django.conf import settings
from django.core.urlresolvers import (
    LocaleRegexURLResolver, get_resolver, get_script_prefix, is_valid_path,
)
from django.http import HttpResponseRedirect
from django.utils import translation
from django.utils.cache import patch_vary_headers
# Override the Django 1.4 implementation with the 1.8 implementation
from django_locale.trans_real import get_language_from_request


class LocaleMiddleware(object):
    """
    This is a very simple middleware that parses a request
    and decides what translation object to install in the current
    thread context. This allows pages to be dynamically
    translated to the language the user desires (if the language
    is available, of course).
    """
    response_redirect_class = HttpResponseRedirect

    def __init__(self):
        self._is_language_prefix_patterns_used = False
        for url_pattern in get_resolver(None).url_patterns:
            if isinstance(url_pattern, LocaleRegexURLResolver):
                self._is_language_prefix_patterns_used = True
                break

    def process_request(self, request):
        print 'welcome to django_locale.middleware'
        check_path = self.is_language_prefix_patterns_used()
        # This call is broken in Django 1.4:
        # https://github.com/django/django/blob/stable/1.4.x/django/utils/translation/trans_real.py#L399
        # (we override parse_accept_lang_header to a fixed version in dark_lang.middleware)
        language = get_language_from_request(
            request, check_path=check_path)
        print 'Returning from get_language_from_request; activating language', language
        translation.activate(language)
        request.LANGUAGE_CODE = translation.get_language()
        print 'in process_request, lang code:', request.LANGUAGE_CODE

    def process_response(self, request, response):
        language = translation.get_language()
        language_from_path = translation.get_language_from_path(request.path_info)
        if (response.status_code == 404 and not language_from_path
                and self.is_language_prefix_patterns_used()):
            urlconf = getattr(request, 'urlconf', None)
            language_path = '/%s%s' % (language, request.path_info)
            path_valid = is_valid_path(language_path, urlconf)
            if (not path_valid and settings.APPEND_SLASH
                    and not language_path.endswith('/')):
                path_valid = is_valid_path("%s/" % language_path, urlconf)

            if path_valid:
                script_prefix = get_script_prefix()
                language_url = "%s://%s%s" % (
                    request.scheme,
                    request.get_host(),
                    # insert language after the script prefix and before the
                    # rest of the URL
                    request.get_full_path().replace(
                        script_prefix,
                        '%s%s/' % (script_prefix, language),
                        1
                    )
                )
                return self.response_redirect_class(language_url)

        if not (self.is_language_prefix_patterns_used()
                and language_from_path):
            patch_vary_headers(response, ('Accept-Language',))
        if 'Content-Language' not in response:
            response['Content-Language'] = language
        return response

    def is_language_prefix_patterns_used(self):
        """
        Returns `True` if the `LocaleRegexURLResolver` is used
        at root level of the urlpatterns, else it returns `False`.
        """
        return self._is_language_prefix_patterns_used
