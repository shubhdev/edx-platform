""" Contains the APIs for course credit requirements """

from .exceptions import InvalidCreditRequirements
from .models import CreditCourse, CreditRequirement, CreditRequirementStatus, CreditEligibility
from openedx.core.djangoapps.credit.exceptions import InvalidCreditCourse


def set_credit_requirements(course_key, requirements):
    """Add requirements to given course.

    Args:
        course_key(CourseKey): The identifier for course
        requirements(list): List of requirements to be added

    Example:
        >>> set_credit_requirements(
                "course-v1-edX-DemoX-1T2015",
                [
                    {
                        "namespace": "reverification",
                        "name": "i4x://edX/DemoX/edx-reverification-block/assessment_uuid",
                        "display_name": "Assessment 1",
                        "criteria": {},
                    },
                    {
                        "namespace": "proctored_exam",
                        "name": "i4x://edX/DemoX/proctoring-block/final_uuid",
                        "display_name": "Final Exam",
                        "criteria": {},
                    },
                    {
                        "namespace": "grade",
                        "name": "grade",
                        "display_name": "Grade",
                        "criteria": {"min_grade": 0.8},
                    },
                ])

    Raises:
        InvalidCreditRequirements

    Returns:
        None
    """

    invalid_requirements = _validate_requirements(requirements)
    if invalid_requirements:
        invalid_requirements = ", ".join(invalid_requirements)
        raise InvalidCreditRequirements(invalid_requirements)

    try:
        credit_course = CreditCourse.get_credit_course(course_key=course_key)
    except CreditCourse.DoesNotExist:
        raise InvalidCreditCourse()

    old_requirements = CreditRequirement.get_course_requirements(course_key=course_key)
    requirements_to_disable = _get_requirements_to_disable(old_requirements, requirements)
    if requirements_to_disable:
        CreditRequirement.disable_credit_requirements(requirements_to_disable)

    for requirement in requirements:
        CreditRequirement.add_or_update_course_requirement(credit_course, requirement)


def get_credit_requirements(course_key, namespace=None):
    """Get credit eligibility requirements of a given course and namespace.

    Args:
        course_key(CourseKey): The identifier for course
        namespace(str): Namespace of requirements

    Example:
        >>> get_credit_requirements("course-v1-edX-DemoX-1T2015")
                {
                    requirements =
                    [
                        {
                            "namespace": "reverification",
                            "name": "i4x://edX/DemoX/edx-reverification-block/assessment_uuid",
                            "display_name": "Assessment 1",
                            "criteria": {},
                        },
                        {
                            "namespace": "proctored_exam",
                            "name": "i4x://edX/DemoX/proctoring-block/final_uuid",
                            "display_name": "Final Exam",
                            "criteria": {},
                        },
                        {
                            "namespace": "grade",
                            "name": "grade",
                            "display_name": "Grade",
                            "criteria": {"min_grade": 0.8},
                        },
                    ]
                }

    Returns:
        Dict of requirements in the given namespace
    """

    requirements = CreditRequirement.get_course_requirements(course_key, namespace)
    return [
        {
            "namespace": requirement.namespace,
            "name": requirement.name,
            "display_name": requirement.display_name,
            "criteria": requirement.criteria
        }
        for requirement in requirements
    ]


def get_credit_requirement_status(course_key, username):
    """ Retrieve the user's status for each credit requirement in the course.

    Args:
        course_key (CourseKey): The identifier for course
        username (str): The identifier of the user

    Example:
        >>> get_credit_requirement_status("course-v1-edX-DemoX-1T2015", "john")

                [
                    {
                        "namespace": "verification",
                        "name": "verification",
                        "criteria": {},
                        "status": "satisfied",
                    },
                    {
                        "namespace": "reverification",
                        "name": "midterm",
                        "criteria": {},
                        "status": "Not satisfied",
                    },
                    {
                        "namespace": "proctored_exam",
                        "name": "final",
                        "criteria": {},
                        "status": "error",
                    },
                    {
                        "namespace": "grade",
                        "name": "grade",
                        "criteria": {"min_grade": 0.8},
                        "status": None,
                    },
                ]

    Returns:
        list of requirement statuses
    """
    requirements = CreditRequirement.get_course_requirements(course_key)
    requirement_list = [requirement.id for requirement in requirements]
    requirement_statuses = CreditRequirementStatus.get_statuses(requirement_list, username)
    statuses = []
    for requirement in requirements:
        status = None
        status_date = None
        for requirement_status in requirement_statuses:
            if requirement_status.requirement == requirement:
                status = requirement_status.status
                status_date = requirement_status.modified.strftime('%m/%d/%Y')
                break
        statuses.append({
            "namespace": requirement.namespace,
            "name": requirement.name,
            "criteria": requirement.criteria,
            "status": status,
            "status_date": status_date,
        })
    return statuses


def is_course_credit_eligible(username, course_key):
    """Check if the given user is eligible for provided course

    Args:
        username(str): The identifier for user
        course_key (CourseKey): The identifier for course

    Returns:
        True if user is eligible for the course else False
    """
    return CreditEligibility.is_credit_course(course_key, username)


def is_credit_course(course_key):
    """Check if the given course is a credit course

    Arg:
        course_key (CourseKey): The identifier for course

    Returns:
        True if course is credit course else False
    """
    return CreditCourse.is_credit_course(course_key)


def _get_requirements_to_disable(old_requirements, new_requirements):
    """Get the ids of 'CreditRequirement' entries to be disabled that are
    deleted from the courseware.

    Args:
        old_requirements(QuerySet): QuerySet of CreditRequirement
        new_requirements(list): List of requirements being added

    Returns:
        List of ids of CreditRequirement that are not in new_requirements
    """
    requirements_to_disable = []
    for old_req in old_requirements:
        found_flag = False
        for req in new_requirements:
            # check if an already added requirement is modified
            if req["namespace"] == old_req.namespace and req["name"] == old_req.name:
                found_flag = True
                break
        if not found_flag:
            requirements_to_disable.append(old_req.id)
    return requirements_to_disable


def _validate_requirements(requirements):
    """Validate the requirements.

    Args:
        requirements(list): List of requirements

    Returns:
        List of strings of invalid requirements
    """
    invalid_requirements = []
    for requirement in requirements:
        invalid_params = []
        if not requirement.get("namespace"):
            invalid_params.append("namespace")
        if not requirement.get("name"):
            invalid_params.append("name")
        if not requirement.get("display_name"):
            invalid_params.append("display_name")
        if "criteria" not in requirement:
            invalid_params.append("criteria")

        if invalid_params:
            invalid_requirements.append(
                u"{requirement} has missing/invalid parameters: {params}".format(
                    requirement=requirement,
                    params=invalid_params,
                )
            )
    return invalid_requirements
