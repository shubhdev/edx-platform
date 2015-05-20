"""
Tests for tasks.
"""
import ddt

from xmodule.modulestore.django import modulestore
from xmodule.modulestore.tests.factories import CourseFactory, ItemFactory
from xmodule.modulestore.tests.django_utils import ModuleStoreTestCase

from student.tests.factories import AdminFactory

from ..models import XBlockCache
from ..tasks import _calculate_course_xblocks_data, _update_xblocks_cache


@ddt.ddt
class XBlockCacheTaskTests(ModuleStoreTestCase):
    """
    Test the Bookmark model.
    """
    def setUp(self):
        super(XBlockCacheTaskTests, self).setUp()

        self.admin = AdminFactory()

        self.course = CourseFactory.create(display_name='An Introduction to API Testing')

        self.chapter = ItemFactory.create(
            parent_location=self.course.location, category='chapter', display_name='Week 1'
        )
        self.sequential1 = ItemFactory.create(
            parent_location=self.chapter.location, category='sequential', display_name='Lesson 1'
        )
        self.sequential2 = ItemFactory.create(
            parent_location=self.chapter.location, category='sequential', display_name='Lesson 2'
        )
        self.vertical1 = ItemFactory.create(
            parent_location=self.sequential1.location, category='vertical', display_name='Subsection 1'
        )
        self.vertical2 = ItemFactory.create(
            parent_location=self.sequential1.location, category='vertical', display_name='Subsection 2'
        )

        # Vertical2 has two parents
        self.sequential2.children.append(self.vertical2.location)
        modulestore().update_item(self.sequential2, self.admin.id)  # pylint: disable=no-member

        self.expected_cache_data = {
            self.course.location: [
                [],
            ], self.chapter.location: [
                [
                    self.course.location,
                ],
            ], self.sequential1.location: [
                [
                    self.course.location,
                    self.chapter.location,
                ],
            ], self.sequential2.location: [
                [
                    self.course.location,
                    self.chapter.location,
                ],
            ], self.vertical1.location: [
                [
                    self.course.location,
                    self.chapter.location,
                    self.sequential1.location,
                ],
            ], self.vertical2.location: [
                [
                    self.course.location,
                    self.chapter.location,
                    self.sequential1.location,
                ],
                [
                    self.course.location,
                    self.chapter.location,
                    self.sequential2.location,
                ],
            ],
        }

    def test_calculate_course_xblocks_data(self):

        blocks_data = _calculate_course_xblocks_data(self.course.id)

        for usage_key, __ in self.expected_cache_data.items():
            for path_index, path in enumerate(blocks_data[unicode(usage_key)]['paths']):
                for node_index, node in enumerate(path):
                    self.assertEqual(
                        node['usage_key'], self.expected_cache_data[usage_key][path_index][node_index]
                    )

    def test_update_xblocks_cache(self):

        _update_xblocks_cache(self.course.id)

        for usage_key, __ in self.expected_cache_data.items():
            xblock_cache = XBlockCache.objects.get(usage_key=usage_key)
            for path_index, path in enumerate(xblock_cache.paths):
                for path_item_index, path_item in enumerate(path):
                    self.assertEqual(
                        path_item.usage_key, self.expected_cache_data[usage_key][path_index][path_item_index]
                    )
