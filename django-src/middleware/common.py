# -*- coding:UTF-8 -*-

import re
import warnings
from urllib.parse import urlparse

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.core.mail import mail_managers
from django.http import HttpResponsePermanentRedirect
from django.urls import is_valid_path
from django.utils.cache import (
    cc_delim_re, get_conditional_response, set_response_etag,
)
from django.utils.deprecation import MiddlewareMixin, RemovedInDjango21Warning


class CommonMiddleware(MiddlewareMixin):
    """
    "Common" middleware for taking care of some basic operations:

        - Forbid access to User-Agents in settings.DISALLOWED_USER_AGENTS

        - URL rewriting: Based on the APPEND_SLASH and PREPEND_WWW settings,
          append missing slashes and/or prepends missing "www."s.

            - If APPEND_SLASH is set and the initial URL doesn't end with a
              slash, and it is not found in urlpatterns, form a new URL by
              appending a slash at the end. If this new URL is found in
              urlpatterns, return an HTTP redirect to this new URL; otherwise
              process the initial URL as usual.

          This behavior can be customized by subclassing CommonMiddleware and
          overriding the response_redirect_class attribute.

        - ETags: If the USE_ETAGS setting is set, ETags will be calculated from
          the entire page content and Not Modified responses will be returned
          appropriately. USE_ETAGS is deprecated in favor of
          ConditionalGetMiddleware.
    """

    response_redirect_class = HttpResponsePermanentRedirect

    def process_request(self, request):
        """
        Check for denied User-Agents and rewrite the URL based on
        settings.APPEND_SLASH and settings.PREPEND_WWW
        """

        # Check for denied User-Agents
        if 'HTTP_USER_AGENT' in request.META:
            for user_agent_regex in settings.DISALLOWED_USER_AGENTS:
                if user_agent_regex.search(request.META['HTTP_USER_AGENT']):
                    raise PermissionDenied('Forbidden user agent')

        # Check for a redirect based on settings.PREPEND_WWW
        host = request.get_host()
        must_prepend = settings.PREPEND_WWW and host and not host.startswith('www.')
        redirect_url = ('%s://www.%s' % (request.scheme, host)) if must_prepend else ''

        # Check if a slash should be appended
        if self.should_redirect_with_slash(request):
            path = self.get_full_path_with_slash(request) # 路径后加/
        else:
            path = request.get_full_path()

        # Return a redirect if necessary
        # 
        if redirect_url or path != request.get_full_path(): # 第二个条件必要吗？必要
            redirect_url += path #输出的是转向url
            return self.response_redirect_class(redirect_url)
        # 其他的结果，没加www并且url没加/, 则url不变

    def should_redirect_with_slash(self, request):
        """
        Return True if settings.APPEND_SLASH is True and appending a slash to
        the request path turns an invalid path into a valid one.
        """
        # 语言语义
        if settings.APPEND_SLASH and not request.path_info.endswith('/'):
            urlconf = getattr(request, 'urlconf', None)
            return (
                not is_valid_path(request.path_info, urlconf) and  #更改前未合法，更改后合法
                is_valid_path('%s/' % request.path_info, urlconf)
            )
        return False

    def get_full_path_with_slash(self, request):
        """
        Return the full path of the request with a trailing slash appended.

        Raise a RuntimeError if settings.DEBUG is True and request.method is
        POST, PUT, or PATCH.
        """
        new_path = request.get_full_path(force_append_slash=True)
        if settings.DEBUG and request.method in ('POST', 'PUT', 'PATCH'):
            # Django不会把post的数据发送到新的url
            raise RuntimeError(
                "You called this URL via %(method)s, but the URL doesn't end "
                "in a slash and you have APPEND_SLASH set. Django can't "
                "redirect to the slash URL while maintaining %(method)s data. "
                "Change your form to point to %(url)s (note the trailing "   
                "slash), or set APPEND_SLASH=False in your Django settings." % {
                    'method': request.method,
                    'url': request.get_host() + new_path,
                }
            )
        return new_path

    def process_response(self, request, response):
        """
        Calculate the ETag, if needed.

        When the status code of the response is 404, it may redirect to a path
        with an appended slash if should_redirect_with_slash() returns True.
        """
        # If the given URL is "Not Found", then check if we should redirect to
        # a path with a slash appended.
        if response.status_code == 404: 
            if self.should_redirect_with_slash(request): # url经过别的插件已经shoud_add_slash?
                return self.response_redirect_class(self.get_full_path_with_slash(request))

        if settings.USE_ETAGS and self.needs_etag(response):
            warnings.warn(
                "The USE_ETAGS setting is deprecated in favor of "
                "ConditionalGetMiddleware which sets the ETag regardless of "
                "the setting. CommonMiddleware won't do ETag processing in "
                "Django 2.1.",
                RemovedInDjango21Warning
            )
            if not response.has_header('ETag'):
                set_response_etag(response)

            if response.has_header('ETag'):
                return get_conditional_response(
                    request,
                    etag=response['ETag'],
                    response=response,
                )
        # Add the Content-Length header to non-streaming responses if not
        # already set.
        if not response.streaming and not response.has_header('Content-Length'):
            response['Content-Length'] = str(len(response.content))

        return response

    # response是否需要一个etag头部
    def needs_etag(self, response): 
        """Return True if an ETag header should be added to response."""
        cache_control_headers = cc_delim_re.split(response.get('Cache-Control', ''))
        return all(header.lower() != 'no-store' for header in cache_control_headers)


# 返回404的url被发送给管理员
class BrokenLinkEmailsMiddleware(MiddlewareMixin):

    def process_response(self, request, response):
        """Send broken link emails for relevant 404 NOT FOUND responses."""
        if response.status_code == 404 and not settings.DEBUG:
            domain = request.get_host()
            path = request.get_full_path()
            referer = request.META.get('HTTP_REFERER', '')

            if not self.is_ignorable_request(request, path, domain, referer):
                ua = request.META.get('HTTP_USER_AGENT', '<none>')
                ip = request.META.get('REMOTE_ADDR', '<none>')
                mail_managers(
                    "Broken %slink on %s" % (
                        ('INTERNAL ' if self.is_internal_request(domain, referer) else ''),
                        domain
                    ),
                    "Referrer: %s\nRequested URL: %s\nUser agent: %s\n"
                    "IP address: %s\n" % (referer, path, ua, ip),
                    fail_silently=True)
        return response

    # 根据正则判断domain是否匹配
    def is_internal_request(self, domain, referer):
        """
        Return True if the referring URL is the same domain as the current
        request.
        """
        # Different subdomains are treated as different domains.
        return bool(re.match("^https?://%s/" % re.escape(domain), referer))

    # 这个request是否可以忽略
    def is_ignorable_request(self, request, uri, domain, referer):
        """
        Return True if the given request *shouldn't* notify the site managers
        according to project settings or in situations outlined by the inline
        comments.
        """
        # The referer is empty.
        if not referer:
            return True

        # APPEND_SLASH is enabled and the referer is equal to the current URL
        # without a trailing slash indicating an internal redirect.
        if settings.APPEND_SLASH and uri.endswith('/') and referer == uri[:-1]:
            return True

        # A '?' in referer is identified as a search engine source.
        if not self.is_internal_request(domain, referer) and '?' in referer:
            return True

        # The referer is equal to the current URL, ignoring the scheme (assumed
        # to be a poorly implemented bot).
        parsed_referer = urlparse(referer)
        if parsed_referer.netloc in ['', domain] and parsed_referer.path == uri:
            return True

        return any(pattern.search(uri) for pattern in settings.IGNORABLE_404_URLS) #这样的url被忽略
