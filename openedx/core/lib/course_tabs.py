"""
Tabs for courseware.
"""
from openedx.core.lib.api.plugins import PluginManager

_ = lambda text: text


# Stevedore extension point namespaces
COURSE_TAB_NAMESPACE = 'openedx.course_tab'


class CourseTabPluginManager(PluginManager):
    """
    Manager for all of the course tabs that have been made available.

    All course tabs should implement `CourseTab`.
    """
    NAMESPACE = COURSE_TAB_NAMESPACE

    @classmethod
    def get_tab_types(cls):
        """
        Returns the list of available course tabs in their canonical order.
        """
        def compare_tabs(first_type, second_type):
            """Compares two course tabs, for use in sorting."""
            first_priority = first_type.priority
            second_priority = second_type.priority
            if not first_priority == second_priority:
                if not first_priority:
                    return 1
                elif not second_priority:
                    return -1
                else:
                    return first_priority - second_priority
            first_name = first_type.type
            second_name = second_type.type
            if first_name < second_name:
                return -1
            elif first_name == second_name:
                return 0
            else:
                return 1
        tab_types = cls.get_available_plugins().values()
        tab_types.sort(cmp=compare_tabs)
        return tab_types
