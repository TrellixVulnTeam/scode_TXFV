# -*- coding:UTF-8 -*-

import django
from django.core.handlers.wsgi import WSGIHandler

# 返回可调用应用端
def get_wsgi_application():
    """
    The public interface to Django's WSGI support. Return a WSGI callable.

    Avoids making django.core.handlers.WSGIHandler a public API, in case the
    internal WSGI implementation changes or moves in the future.
    """
    django.setup(set_prefix=False)
    return WSGIHandler()
