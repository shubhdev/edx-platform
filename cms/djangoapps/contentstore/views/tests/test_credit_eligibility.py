"""
Unit tests for credit eligibility UI in Studio.
"""

from mock import patch

from contentstore.tests.utils import CourseTestCase
from contentstore.utils import reverse_course_url
from xmodule.modulestore.tests.factories import CourseFactory

from openedx.core.djangoapps.credit.models import CreditCourse


class CreditEligibilityTest(CourseTestCase):
    """Base class to test the course settings details view in Studio for credit
    eligibility.
    """
    def setUp(self, **kwargs):
        enable_credit_eligibility = kwargs.get('enable_credit_eligibility', False)
        with patch.dict('django.conf.settings.FEATURES', {'ENABLE_CREDIT_ELIGIBILITY': enable_credit_eligibility}):
            super(CreditEligibilityTest, self).setUp()

        self.course = CourseFactory.create(org='edX', number='dummy', display_name='Credit Course')
        self.course_details_url = reverse_course_url('settings_handler', unicode(self.course.id))


class CreditEligibilityDisabledTest(CreditEligibilityTest):
    """Test the course settings details view response when feature flag
    'ENABLE_CREDIT_ELIGIBILITY' is not enabled.
    """
    def setUp(self):
        super(CreditEligibilityDisabledTest, self).setUp(enable_credit_eligibility=False)

    def test_get_method_without_enable_feature_flag(self):
        """Test that user don't see credit eligibility requirements in response
        if the feature flag 'ENABLE_CREDIT_ELIGIBILITY' is not enabled.
        """
        response = self.client.get_html(self.course_details_url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Credit Eligibility Requirements")
        self.assertNotContains(response, "Steps needed for credit eligibility")


class CreditEligibilityEnabledTest(CreditEligibilityTest):
    """Test the course settings details view response when feature flag
    'ENABLE_CREDIT_ELIGIBILITY' is enabled.
    """
    def setUp(self):
        super(CreditEligibilityEnabledTest, self).setUp(enable_credit_eligibility=True)

    def test_get_method(self):
        """Test that credit eligibility requirements are present in
        response if the feature flag 'ENABLE_CREDIT_ELIGIBILITY' is enabled.
        """
        # verify that credit eligibility requirements block don't show if the
        # course is not set as credit course
        response = self.client.get_html(self.course_details_url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Credit Eligibility Requirements")
        self.assertNotContains(response, "Steps needed for credit eligibility")

        # verify that credit eligibility requirements block shows if the
        # course is set as credit course
        credit_course = CreditCourse(course_key=unicode(self.course.id), enabled=True)
        credit_course.save()
        response = self.client.get_html(self.course_details_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Credit Eligibility Requirements")
        self.assertContains(response, "Steps needed for credit eligibility")
