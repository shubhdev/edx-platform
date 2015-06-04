"""
Defines the URL routes for this app.
"""
from django.conf import settings
from django.conf.urls import patterns, url

from openedx.core.djangoapps.credit.tests.fake_update_min_grade import(
    UpdateMinGradeRequirementFakeView
)

urlpatterns = patterns(
    '',
)

if settings.FEATURES.get('ENABLE_MIN_GRADE_STATUS_UPDATE'):
    urlpatterns += (url(
        r'^check_grade',
        UpdateMinGradeRequirementFakeView.as_view(),
        name='UpdateMinRequirementFakeView'
    ),)
