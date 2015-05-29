 # -*- coding: utf-8 -*-
"""
Tests for credit course models.
"""

import ddt
from django.test import TestCase

from opaque_keys.edx.keys import CourseKey

from openedx.core.djangoapps.credit.models import (
    CreditCourse,
    CreditProvider,
    CreditRequirement,
    CreditRequest,
    CreditRequestStatus,
)


@ddt.ddt
class CreditEligibilityModelTests(TestCase):
    """
    Tests for credit models used to track credit eligibility.
    """

    def setUp(self, **kwargs):
        super(CreditEligibilityModelTests, self).setUp()
        self.course_key = CourseKey.from_string("edX/DemoX/Demo_Course")

    @ddt.data(False, True)
    def test_is_credit_course(self, is_credit):
        CreditCourse(course_key=self.course_key, enabled=is_credit).save()
        if is_credit:
            self.assertTrue(CreditCourse.is_credit_course(self.course_key))
        else:
            self.assertFalse(CreditCourse.is_credit_course(self.course_key))

    def test_get_course_requirements(self):
        credit_course = self.add_credit_course()
        requirement = {
            "namespace": "grade",
            "name": "grade",
            "display_name": "Grade",
            "criteria": {
                "min_grade": 0.8
            }
        }
        credit_req, created = CreditRequirement.add_or_update_course_requirement(credit_course, requirement)
        self.assertEqual(credit_course, credit_req.course)
        self.assertEqual(created, True)
        requirements = CreditRequirement.get_course_requirements(self.course_key)
        self.assertEqual(len(requirements), 1)

    def test_add_course_requirement_namespace(self):
        credit_course = self.add_credit_course()
        requirement = {
            "namespace": "grade",
            "name": "grade",
            "display_name": "Grade",
            "criteria": {
                "min_grade": 0.8
            }
        }
        credit_req, created = CreditRequirement.add_or_update_course_requirement(credit_course, requirement)
        self.assertEqual(credit_course, credit_req.course)
        self.assertEqual(created, True)

        requirement = {
            "namespace": "reverification",
            "name": "i4x://edX/DemoX/edx-reverification-block/assessment_uuid",
            "display_name": "Assessment 1",
            "criteria": {}
        }
        credit_req, created = CreditRequirement.add_or_update_course_requirement(credit_course, requirement)
        self.assertEqual(credit_course, credit_req.course)
        self.assertEqual(created, True)

        requirements = CreditRequirement.get_course_requirements(self.course_key)
        self.assertEqual(len(requirements), 2)

        requirements = CreditRequirement.get_course_requirements(self.course_key, namespace="grade")
        self.assertEqual(len(requirements), 1)

    def add_credit_course(self):
        """ Add the course as a credit

        Returns:
            CreditCourse object
        """
        credit_course = CreditCourse(course_key=self.course_key, enabled=True)
        credit_course.save()
        return credit_course


@ddt.ddt
class CreditRequestModelTests(TestCase):
    """
    Tests for models used to track credit requests and status.
    """

    USERNAME = u"rön"
    PROVIDER_ID = u"høgwarts"

    def setUp(self):
        """Create a course and provider. """
        super(CreditRequestModelTests, self).setUp()
        self.course_key = CourseKey.from_string("edX/DemoX/Demo_Course")
        self.credit_course = CreditCourse.objects.create(course_key=self.course_key)
        self.credit_provider = CreditProvider.objects.create(provider_id=self.PROVIDER_ID)

    def test_credit_request_current_status(self):
        request = CreditRequest.objects.create(
            uuid="abcd1234",
            username=self.USERNAME,
            course=self.credit_course,
            provider=self.credit_provider,
        )

        # Initially, status should be pending
        self.assertEqual(request.current_status(), "pending")

        # Update the status
        CreditRequestStatus.objects.create(request=request, status="approved")
        self.assertEqual(request.current_status(), "approved")

        # Update the status again
        CreditRequestStatus.objects.create(request=request, status="rejected")
        self.assertEqual(request.current_status(), "rejected")

    def test_credit_request_all_requests_for_user(self):
        # Before issuing a request, this should be an empty list
        requests = CreditRequest.credit_requests_for_user(self.USERNAME)
        self.assertEqual(requests, [])

        # Create a new request
        request = CreditRequest.objects.create(
            uuid="abcd1234",
            username=self.USERNAME,
            course=self.credit_course,
            provider=self.credit_provider,
        )

        # Initially, the status should be "pending"
        requests = CreditRequest.credit_requests_for_user(self.USERNAME)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["status"], "pending")

        # Update the status.  The more recent status should be returned
        CreditRequestStatus.objects.create(request=request, status="approved")
        requests = CreditRequest.credit_requests_for_user(self.USERNAME)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["status"], "approved")

    def test_multiple_credit_requests_for_user(self):
        other_course_key = CourseKey.from_string("edX/DemoX/OtherCourse")
        other_course = CreditCourse.objects.create(course_key=other_course_key)

        # Initiate two requests for the same user
        CreditRequest.objects.create(
            uuid="abcd1234",
            username=self.USERNAME,
            course=self.credit_course,
            provider=self.credit_provider,
        )

        second_request = CreditRequest.objects.create(
            uuid="efghi6789",
            username=self.USERNAME,
            course=other_course,
            provider=self.credit_provider,
        )

        # Approve the second request
        CreditRequestStatus.objects.create(request=second_request, status="approved")

        # Retrieve all requests and check the status
        requests = CreditRequest.credit_requests_for_user(self.USERNAME)
        self.assertEqual(len(requests), 2)

        # Assume that the entries are sorted in ascending alphabetical
        # order by (course_key, provider_id)
        self.assertEqual(requests[0]["course_key"], unicode(self.course_key))
        self.assertEqual(requests[0]["status"], "pending")
        self.assertEqual(requests[1]["course_key"], unicode(other_course_key))
        self.assertEqual(requests[1]["status"], "approved")
