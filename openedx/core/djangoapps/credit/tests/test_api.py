"""
Tests for the API functions in the credit app.
"""

import datetime
import ddt
import pytz
import dateutil.parser as date_parser
from django.test import TestCase
from django.db import connection, transaction

from opaque_keys.edx.keys import CourseKey

from student.tests.factories import UserFactory
from openedx.core.djangoapps.credit import api
from openedx.core.djangoapps.credit.exceptions import (
    InvalidCreditRequirements,
    InvalidCreditCourse,
    RequestAlreadyCompleted,
    UserIsNotEligible,
    InvalidCreditStatus,
    CreditRequestNotFound,
)
from openedx.core.djangoapps.credit.models import (
    CreditCourse,
    CreditProvider,
    CreditRequirement,
    CreditRequirementStatus,
    CreditEligibility,
)


class CreditApiTestBase(TestCase):
    """
    Base class for test cases of the credit API.
    """

    PROVIDER_ID = "hogwarts"
    PROVIDER_NAME = "Hogwarts School of Witchcraft and Wizardry"

    def setUp(self, **kwargs):
        super(CreditApiTestBase, self).setUp()
        self.course_key = CourseKey.from_string("edX/DemoX/Demo_Course")

    def add_credit_course(self, enabled=True):
        """Mark the course as a credit """
        credit_course = CreditCourse.objects.create(course_key=self.course_key, enabled=enabled)
        credit_course.save()

        # Associate a credit provider with the course.
        credit_provider = CreditProvider(provider_id=self.PROVIDER_ID, display_name=self.PROVIDER_NAME)
        credit_provider.save()
        credit_course.providers.add(credit_provider)

        return credit_course


@ddt.ddt
class CreditRequirementApiTests(CreditApiTestBase):
    """
    Test Python API for credit requirements and eligibility.
    """

    @ddt.data(
        [
            {
                "namespace": "grade",
                "criteria": {
                    "min_grade": 0.8
                }
            }
        ],
        [
            {
                "name": "grade",
                "criteria": {
                    "min_grade": 0.8
                }
            }
        ],
        [
            {
                "namespace": "grade",
                "name": "grade",
                "display_name": "Grade"
            }
        ]
    )
    def test_set_credit_requirements_invalid_requirements(self, requirements):
        self.add_credit_course()
        with self.assertRaises(InvalidCreditRequirements):
            api.set_credit_requirements(self.course_key, requirements)

    def test_set_credit_requirements_invalid_course(self):
        requirements = [
            {
                "namespace": "grade",
                "name": "grade",
                "display_name": "Grade",
                "criteria": {}
            }
        ]
        with self.assertRaises(InvalidCreditCourse):
            api.set_credit_requirements(self.course_key, requirements)

        self.add_credit_course(enabled=False)
        with self.assertRaises(InvalidCreditCourse):
            api.set_credit_requirements(self.course_key, requirements)

    def test_set_get_credit_requirements(self):
        self.add_credit_course()
        requirements = [
            {
                "namespace": "grade",
                "name": "grade",
                "display_name": "Grade",
                "criteria": {
                    "min_grade": 0.8
                }
            },
            {
                "namespace": "grade",
                "name": "grade",
                "display_name": "Grade",
                "criteria": {
                    "min_grade": 0.9
                }
            }
        ]
        api.set_credit_requirements(self.course_key, requirements)
        self.assertEqual(len(api.get_credit_requirements(self.course_key)), 1)

    def test_disable_credit_requirements(self):
        self.add_credit_course()
        requirements = [
            {
                "namespace": "grade",
                "name": "grade",
                "display_name": "Grade",
                "criteria": {
                    "min_grade": 0.8
                }
            }
        ]
        api.set_credit_requirements(self.course_key, requirements)
        self.assertEqual(len(api.get_credit_requirements(self.course_key)), 1)

        requirements = [
            {
                "namespace": "reverification",
                "name": "i4x://edX/DemoX/edx-reverification-block/assessment_uuid",
                "display_name": "Assessment 1",
                "criteria": {}
            }
        ]
        api.set_credit_requirements(self.course_key, requirements)
        self.assertEqual(len(api.get_credit_requirements(self.course_key)), 1)

        grade_req = CreditRequirement.objects.filter(namespace="grade", name="grade")
        self.assertEqual(len(grade_req), 1)
        self.assertEqual(grade_req[0].active, False)

    def test_requirements_to_disable(self):
        self.add_credit_course()
        requirements = [
            {
                "namespace": "grade",
                "name": "grade",
                "display_name": "Grade",
                "criteria": {
                    "min_grade": 0.8
                }
            }
        ]


@ddt.ddt
class CreditProviderIntegrationApiTests(CreditApiTestBase):
    """
    Test Python API for credit provider integration.
    """

    USER_INFO = {
        "username": "bob",
        "email": "bob@example.com",
        "full_name": "Bob",
        "mailing_address": "123 Fake Street, Cambridge MA",
        "country": "US",
    }

    FINAL_GRADE = 0.95

    def setUp(self):
        super(CreditProviderIntegrationApiTests, self).setUp()
        self.user = UserFactory(
            username=self.USER_INFO['username'],
            email=self.USER_INFO['email'],
        )

        self.user.profile.name = self.USER_INFO['full_name']
        self.user.profile.mailing_address = self.USER_INFO['mailing_address']
        self.user.profile.country = self.USER_INFO['country']
        self.user.profile.save()

    def test_credit_request(self):
        self._configure_credit()

        # Initiate a credit request
        request = api.create_credit_request(self.course_key, self.PROVIDER_ID, self.USER_INFO['username'])

        # Validate the UUID
        self.assertIn('uuid', request)
        self.assertEqual(len(request['uuid']), 32)

        # Validate the timestamp
        self.assertIn('timestamp', request)
        parsed_date = date_parser.parse(request['timestamp'])
        self.assertTrue(parsed_date < datetime.datetime.now(pytz.UTC))

        # Validate course information
        self.assertIn('course_org', request)
        self.assertEqual(request['course_org'], self.course_key.org)
        self.assertIn('course_num', request)
        self.assertEqual(request['course_num'], self.course_key.course)
        self.assertIn('course_run', request)
        self.assertEqual(request['course_run'], self.course_key.run)
        self.assertIn('final_grade', request)
        self.assertEqual(request['final_grade'], self.FINAL_GRADE)

        # Validate user information
        for key in self.USER_INFO.keys():
            request_key = 'user_{key}'.format(key=key)
            self.assertIn(request_key, request)
            self.assertEqual(request[request_key], self.USER_INFO[key])

    @ddt.data("approved", "rejected")
    def test_credit_request_status(self, status):
        self._configure_credit()

        request = api.create_credit_request(self.course_key, self.PROVIDER_ID, self.USER_INFO['username'])

        # Initial status should be "pending"
        self._assert_credit_status("pending")

        # Update the status
        api.update_credit_request_status(request['uuid'], status)
        self._assert_credit_status(status)

    def test_query_counts(self):
        self._configure_credit()

        # Yes, this is a lot of queries, but this API call is also doing a lot of work :)
        # - 1 query: Check the user's eligibility and retrieve the credit course and provider.
        # - 2 queries: Get-or-create the credit request.
        # - 1 query: Retrieve user account and profile information from the user API.
        # - 1 query: Look up the user's final grade from the credit requirements table.
        # - 2 queries: Update the request.
        # - 1 query: Create the "pending" status object.
        with self.assertNumQueries(8):
            request = api.create_credit_request(self.course_key, self.PROVIDER_ID, self.USER_INFO['username'])

        with self.assertNumQueries(2):
            api.update_credit_request_status(request['uuid'], "approved")

        with self.assertNumQueries(1):
            api.get_credit_requests_for_user(self.USER_INFO['username'])

    def test_reuse_credit_request(self):
        self._configure_credit()

        # Create the first request
        first_request = api.create_credit_request(self.course_key, self.PROVIDER_ID, self.USER_INFO['username'])

        # Update the user's profile information, then attempt a second request
        self.user.profile.name = "Bobby"
        self.user.profile.save()
        second_request = api.create_credit_request(self.course_key, self.PROVIDER_ID, self.USER_INFO['username'])

        # Request UUID should be the same
        self.assertEqual(first_request['uuid'], second_request['uuid'])

        # Request should use the updated information
        self.assertEqual(second_request['user_full_name'], "Bobby")

    @ddt.data("approved", "rejected")
    def test_cannot_make_credit_request_after_response(self, status):
        self._configure_credit()

        # Create the first request
        request = api.create_credit_request(self.course_key, self.PROVIDER_ID, self.USER_INFO['username'])

        # Provider updates the status
        api.update_credit_request_status(request['uuid'], status)

        # Attempting a second request raises an exception
        with self.assertRaises(RequestAlreadyCompleted):
            api.create_credit_request(self.course_key, self.PROVIDER_ID, self.USER_INFO['username'])

    @ddt.data("pending", "failed")
    def test_user_is_not_eligible(self, status):
        self._configure_credit(requirement_status=status)
        with self.assertRaises(UserIsNotEligible):
            api.create_credit_request(self.course_key, self.PROVIDER_ID, self.USER_INFO['username'])

    def test_create_request_null_mailing_address(self):
        self._configure_credit()

        # User did not specify a mailing address
        self.user.profile.mailing_address = None
        self.user.profile.save()

        # Request should include an empty mailing address field
        request = api.create_credit_request(self.course_key, self.PROVIDER_ID, self.USER_INFO['username'])
        self.assertEqual(request["user_mailing_address"], "")

    def test_create_request_null_country(self):
        self._configure_credit()

        # Simulate users who registered accounts before the country field was introduced.
        # We need to manipulate the database directly because the country Django field
        # coerces None values to empty strings.
        query = "UPDATE auth_userprofile SET country = NULL WHERE id = %s"
        connection.cursor().execute(query, [str(self.user.profile.id)])
        transaction.commit_unless_managed()

        # Request should include an empty country field
        request = api.create_credit_request(self.course_key, self.PROVIDER_ID, self.USER_INFO['username'])
        self.assertEqual(request["user_country"], "")

    def test_user_has_no_final_grade(self):
        self._configure_credit()

        # Simulate an error condition that should never happen:
        # a user is eligible for credit, but doesn't have a final
        # grade recorded in the eligibility requirement.
        grade_status = CreditRequirementStatus.objects.get(
            username=self.USER_INFO['username'],
            requirement__namespace="grade",
            requirement__name="grade"
        )
        grade_status.reason = {}
        grade_status.save()

        with self.assertRaises(UserIsNotEligible):
            api.create_credit_request(self.course_key, self.PROVIDER_ID, self.USER_INFO['username'])

    def test_update_invalid_credit_status(self):
        self._configure_credit()
        request = api.create_credit_request(self.course_key, self.PROVIDER_ID, self.USER_INFO['username'])
        with self.assertRaises(InvalidCreditStatus):
            api.update_credit_request_status(request['uuid'], "invalid")

    def test_update_credit_request_not_found(self):
        self._configure_credit()
        with self.assertRaises(CreditRequestNotFound):
            api.update_credit_request_status("invalid_uuid", "approved")

    def test_get_credit_requests_no_requests(self):
        self._configure_credit()
        requests = api.get_credit_requests_for_user(self.USER_INFO['username'])
        self.assertEqual(requests, [])

    def _configure_credit(self, requirement_status="satisfied"):
        """Configure a credit course and its requirements. """
        credit_course = self.add_credit_course()
        requirement = CreditRequirement.objects.create(
            course=credit_course,
            namespace="grade",
            name="grade",
            active=True
        )
        status = CreditRequirementStatus.objects.create(
            username=self.USER_INFO['username'],
            requirement=requirement,
        )
        status.status = requirement_status
        if requirement_status == "satisfied":
            status.reason = {"final_grade": self.FINAL_GRADE}

        status.save()

        if requirement_status == "satisfied":
            CreditEligibility.objects.create(
                username=self.USER_INFO['username'],
                course=CreditCourse.objects.get(course_key=self.course_key),
                provider=CreditProvider.objects.get(provider_id=self.PROVIDER_ID)
            )

    def _assert_credit_status(self, expected_status):
        """Check the user's credit status. """
        statuses = api.get_credit_requests_for_user(self.USER_INFO['username'])
        self.assertEqual(statuses[0]["status"], expected_status)
