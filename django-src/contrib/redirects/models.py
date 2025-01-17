#! /usr/bin/env python
# -*- coding:UTF-8 -*-

from django.contrib.sites.models import Site
from django.db import models
from django.utils.translation import gettext_lazy as _


class Redirect(models.Model):
    site = models.ForeignKey(Site, models.CASCADE, verbose_name=_('site'))
    old_path = models.CharField(
        _('redirect from'),
        max_length=200,
        db_index=True,
        help_text=_("This should be an absolute path, excluding the domain name. Example: '/events/search/'."),
    )
    new_path = models.CharField(
        _('redirect to'),
        max_length=200,
        blank=True,
        help_text=_("This can be either an absolute path (as above) or a full URL starting with 'http://'."),
    )

    class Meta:
        verbose_name = _('redirect')
        verbose_name_plural = _('redirects')
        db_table = 'django_redirect'
        unique_together = (('site', 'old_path'),)  # 这里应用这个属性了!
        ordering = ('old_path',)

    def __str__(self):
        return "%s ---> %s" % (self.old_path, self.new_path)
