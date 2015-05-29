# -*- coding: utf-8 -*-
"""
Models for Credit Eligibility for courses.

Credit courses allow students to receive university credit for
successful completion of a course on EdX
"""

import logging

from django.db import models, connection

from jsonfield.fields import JSONField
from model_utils.models import TimeStampedModel
from xmodule_django.models import CourseKeyField
from django.utils.translation import ugettext_lazy


log = logging.getLogger(__name__)


class CreditProvider(TimeStampedModel):
    """This model represents an institution that can grant credit for a course.

    Each provider is identified by unique ID (e.g., 'ASU'). CreditProvider also
    includes a `url` where the student will be sent when he/she will try to
    get credit for course. Eligibility duration will be use to set duration
    for which credit eligible message appears on dashboard.
    """

    provider_id = models.CharField(max_length=255, db_index=True, unique=True)
    display_name = models.CharField(max_length=255)
    provider_url = models.URLField(max_length=255, unique=True)
    eligibility_duration = models.PositiveIntegerField(
        help_text=ugettext_lazy(u"Number of seconds to show eligibility message")
    )
    active = models.BooleanField(default=True)


class CreditCourse(models.Model):
    """
    Model for tracking a credit course.
    """

    course_key = CourseKeyField(max_length=255, db_index=True, unique=True)
    enabled = models.BooleanField(default=False)
    providers = models.ManyToManyField(CreditProvider)

    @classmethod
    def is_credit_course(cls, course_key):
        """Check that given course is credit or not.

        Args:
            course_key(CourseKey): The course identifier

        Returns:
            Bool True if the course is marked credit else False
        """
        return cls.objects.filter(course_key=course_key, enabled=True).exists()

    @classmethod
    def get_credit_course(cls, course_key):
        """Get the credit course if exists for the given 'course_key'.

        Args:
            course_key(CourseKey): The course identifier

        Raises:
            DoesNotExist if no CreditCourse exists for the given course key.

        Returns:
            CreditCourse if one exists for the given course key.
        """
        return cls.objects.get(course_key=course_key, enabled=True)


class CreditRequirement(TimeStampedModel):
    """
    This model represents a credit requirement.

    Each requirement is uniquely identified by its 'namespace' and
    'name' fields.
    The 'name' field stores the unique name or location (in case of XBlock)
    for a requirement, which serves as the unique identifier for that
    requirement.
    The 'display_name' field stores the display name of the requirement.
    The 'criteria' field dictionary provides additional information, clients
    may need to determine whether a user has satisfied the requirement.
    """

    course = models.ForeignKey(CreditCourse, related_name="credit_requirements")
    namespace = models.CharField(max_length=255)
    name = models.CharField(max_length=255)
    display_name = models.CharField(max_length=255)
    criteria = JSONField()
    active = models.BooleanField(default=True)

    class Meta(object):
        """
        Model metadata.
        """
        unique_together = ('namespace', 'name', 'course')

    @classmethod
    def add_or_update_course_requirement(cls, credit_course, requirement):
        """
        Add requirement to a given course.

        Args:
            credit_course(CreditCourse): The identifier for credit course
            requirement(dict): Requirement dict to be added

        Returns:
            (CreditRequirement, created) tuple
        """

        credit_requirement, created = cls.objects.get_or_create(
            course=credit_course,
            namespace=requirement["namespace"],
            name=requirement["name"],
            display_name=requirement["display_name"],
            defaults={"criteria": requirement["criteria"], "active": True}
        )
        if not created:
            credit_requirement.criteria = requirement["criteria"]
            credit_requirement.active = True
            credit_requirement.save()

        return credit_requirement, created

    @classmethod
    def get_course_requirements(cls, course_key, namespace=None):
        """
        Get credit requirements of a given course.

        Args:
            course_key(CourseKey): The identifier for a course
            namespace(str): Namespace of credit course requirements

        Returns:
            QuerySet of CreditRequirement model
        """
        requirements = CreditRequirement.objects.filter(course__course_key=course_key, active=True)
        if namespace:
            requirements = requirements.filter(namespace=namespace)
        return requirements

    @classmethod
    def disable_credit_requirements(cls, requirement_ids):
        """
        Mark the given requirements inactive.

        Args:
            requirement_ids(list): List of ids

        Returns:
            None
        """
        cls.objects.filter(id__in=requirement_ids).update(active=False)


class CreditRequirementStatus(TimeStampedModel):
    """
    This model represents the status of each requirement.

    For a particular credit requirement, a user can either:
    1) Have satisfied the requirement (example: approved in-course reverification)
    2) Have failed the requirement (example: denied in-course reverification)
    3) Neither satisfied nor failed (example: the user hasn't yet attempted in-course reverification).

    Cases (1) and (2) are represented by having a CreditRequirementStatus with
    the status set to "satisfied" or "failed", respectively.

    In case (3), no CreditRequirementStatus record will exist for the requirement and user.

    """

    REQUIREMENT_STATUS_CHOICES = (
        ("satisfied", "satisfied"),
        ("failed", "failed"),
    )

    username = models.CharField(max_length=255, db_index=True)
    requirement = models.ForeignKey(CreditRequirement, related_name="statuses")
    status = models.CharField(max_length=32, choices=REQUIREMENT_STATUS_CHOICES)

    # Include additional information about why the user satisfied or failed
    # the requirement.  This is specific to the type of requirement.
    # For example, the minimum grade requirement might record the user's
    # final grade when the user completes the course.  This allows us to display
    # the grade to users later and to send the information to credit providers.
    reason = JSONField(default={})

    class Meta(object):  # pylint: disable=missing-docstring
        get_latest_by = "created"


class CreditEligibility(TimeStampedModel):
    """
    A record of a user's eligibility for credit from a specific credit
    provider for a specific course.
    """

    username = models.CharField(max_length=255, db_index=True)
    course = models.ForeignKey(CreditCourse, related_name="eligibilities")
    provider = models.ForeignKey(CreditProvider, related_name="eligibilities")

    class Meta(object):  # pylint: disable=missing-docstring
        unique_together = ('username', 'course')


class CreditRequest(TimeStampedModel):
    """
    A request for credit from a particular credit provider.

    When a user initiates a request for credit, a CreditRequest record will be created.
    Each CreditRequest is assigned a unique identifier so we can find it when the request
    is approved by the provider.  The CreditRequest record stores the parameters to be sent
    at the time the request is made.  If the user re-issues the request
    (perhaps because the user did not finish filling in forms on the credit provider's site),
    the request record will be updated, but the UUID will remain the same.
    """

    uuid = models.CharField(max_length=32, unique=True, db_index=True)
    username = models.CharField(max_length=255, db_index=True)
    course = models.ForeignKey(CreditCourse, related_name="credit_requests")
    provider = models.ForeignKey(CreditProvider, related_name="credit_requests")
    parameters = JSONField()

    def current_status(self):  # pylint: disable=no-member
        """
        Retrieve the current status for a request.

        This will return either:
        * "pending": The user has initiated a request, but no response has been
            received from the credit provider.
        * "approved": The user's credit request has been approved by the provider.
        * "rejected": The user's credit request has been rejected by the provider.

        """
        try:
            return self.statuses.latest().status  # pylint: disable=no-member
        except CreditRequestStatus.DoesNotExist:
            return "pending"

    @classmethod
    def credit_requests_for_user(cls, username):
        """
        Retrieve all credit requests for a user.

        Arguments:
            username (unicode): The username of the user.

        Returns: list

        Example Usage:
        >>> CreditRequest.credit_requests_for_user("bob")
        [
            {
                "uuid": "557168d0f7664fe59097106c67c3f847",
                "timestamp": "2015-05-04T20:57:57.987119+00:00",
                "course_key": "course-v1:HogwartsX+Potions101+1T2015",
                "provider": {
                    "id": "HogwartsX",
                    "display_name": "Hogwarts School of Witchcraft and Wizardry",
                },
                "status": "pending"  # or "approved" or "rejected"
            }
        ]

        """
        # To minimize the number of database queries, we execute
        # the raw SQL query instead of using Django's ORM.
        # This allows us to use a subquery to
        # retrieve the most recent status for each credit request.
        cursor = connection.cursor()
        cursor.execute(
            (
                'SELECT cr.uuid, cr.created, cc.course_key, cp.provider_id, cp.display_name, '
                'IFNULL(('
                '    SELECT status FROM credit_creditrequeststatus AS crs '
                '    WHERE crs.request_id=cr.id '
                '    ORDER BY created DESC LIMIT 1'
                '), "pending") '
                'FROM credit_creditrequest as cr '
                'LEFT JOIN credit_creditcourse as cc ON cr.course_id=cc.id '
                'LEFT JOIN credit_creditprovider as cp ON cr.provider_id=cp.id '
                'WHERE cr.username=%s '
                'ORDER BY cc.course_key ASC, cp.provider_id ASC '
            ), [username]
        )

        return [
            {
                "uuid": row[0],
                "timestamp": row[1],
                "course_key": row[2],
                "provider": {
                    "id": row[3],
                    "display_name": row[4]
                },
                "status": row[5]
            }
            for row in cursor.fetchall()
        ]

    class Meta(object):  # pylint: disable=missing-docstring
        # Enforce the constraint that each user can have exactly one outstanding
        # request to a given provider.  Multiple requests use the same UUID.
        unique_together = ('username', 'course', 'provider')


class CreditRequestStatus(TimeStampedModel):
    """
    The status of a request for credit.

    For auditing purposes, each time a credit request is issued,
    a CreditRequestStatus is created with status “pending” and a timestamp.

    When a credit request is approved by the credit provider, a CreditRequestStatus
    record will be created and associated with the initiating CreditRequest.
    CreditRequestStatus records are immutable and timestamped.

    The state transitions are:

        [request]--> (pending) --[approved]--> (approved)
                         |
                     [rejected]
                         |
                         V
                     (rejected)

    """

    REQUEST_STATUS_CHOICES = (
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    )

    request = models.ForeignKey(CreditRequest, related_name="statuses")
    status = models.CharField(max_length=255, choices=REQUEST_STATUS_CHOICES)

    class Meta(object):  # pylint: disable=missing-docstring
        get_latest_by = "created"
