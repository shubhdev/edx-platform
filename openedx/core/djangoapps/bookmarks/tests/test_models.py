"""
Tests for Bookmarks models.
"""
import datetime
import ddt
from freezegun import freeze_time
import mock
import pytz

from opaque_keys.edx.keys import UsageKey
from opaque_keys.edx.locator import CourseLocator, BlockUsageLocator
from xmodule.modulestore import ModuleStoreEnum
from xmodule.modulestore.django import modulestore
from xmodule.modulestore.tests.factories import CourseFactory, ItemFactory
from xmodule.modulestore.tests.django_utils import ModuleStoreTestCase

from student.tests.factories import AdminFactory, UserFactory

from .. import DEFAULT_FIELDS, OPTIONAL_FIELDS
from ..models import Bookmark, PathItem, XBlockCache
from .factories import BookmarkFactory


EXAMPLE_USAGE_KEY_1 = u'i4x://org.15/course_15/chapter/Week_1'
EXAMPLE_USAGE_KEY_2 = u'i4x://org.15/course_15/chapter/Week_2'


class BookmarksTestsBase(ModuleStoreTestCase):
    """
    Test the Bookmark model.
    """
    ALL_FIELDS = DEFAULT_FIELDS + OPTIONAL_FIELDS
    STORE_TYPE = ModuleStoreEnum.Type.split
    TEST_PASSWORD = 'test'

    def setUp(self):
        super(BookmarksTestsBase, self).setUp()

        self.admin = AdminFactory()
        self.user = UserFactory.create(password=self.TEST_PASSWORD)
        self.other_user = UserFactory.create(password=self.TEST_PASSWORD)

        with self.store.default_store(self.STORE_TYPE):
            self.course = CourseFactory.create(display_name='An Introduction to API Testing')
            self.course_id = unicode(self.course.id)

            self.chapter_1 = ItemFactory.create(
                parent_location=self.course.location, category='chapter', display_name='Week 1'
            )
            self.chapter_2 = ItemFactory.create(
                parent_location=self.course.location, category='chapter', display_name='Week 2'
            )

            self.sequential_1 = ItemFactory.create(
                parent_location=self.chapter_1.location, category='sequential', display_name='Lesson 1'
            )
            self.sequential_2 = ItemFactory.create(
                parent_location=self.chapter_1.location, category='sequential', display_name='Lesson 2'
            )

            self.vertical_1 = ItemFactory.create(
                parent_location=self.sequential_1.location, category='vertical', display_name='Subsection 1'
            )
            self.vertical_2 = ItemFactory.create(
                parent_location=self.sequential_2.location, category='vertical', display_name='Subsection 2'
            )
            self.vertical_3 = ItemFactory.create(
                parent_location=self.sequential_2.location, category='vertical', display_name='Subsection 3'
            )

        self.path = [
            PathItem(self.chapter_1.location, self.chapter_1.display_name),
            PathItem(self.sequential_2.location, self.sequential_2.display_name),
        ]

        self.bookmark_1 = BookmarkFactory.create(
            user=self.user,
            course_key=self.course_id,
            usage_key=self.sequential_1.location,
            xblock_cache__display_name=self.sequential_1.display_name
        )
        self.bookmark_2 = BookmarkFactory.create(
            user=self.user,
            course_key=self.course_id,
            usage_key=self.sequential_2.location,
            xblock_cache__display_name=self.sequential_2.display_name
        )

        self.other_course = CourseFactory.create(display_name='An Introduction to API Testing 2')

        self.other_chapter_1 = ItemFactory.create(
            parent_location=self.other_course.location, category='chapter', display_name='Other Week 1'
        )
        self.other_sequential_1 = ItemFactory.create(
            parent_location=self.other_chapter_1.location, category='sequential', display_name='Other Lesson 1'
        )
        self.other_sequential_2 = ItemFactory.create(
            parent_location=self.other_chapter_1.location, category='sequential', display_name='Other Lesson 2'
        )
        self.other_vertical_1 = ItemFactory.create(
            parent_location=self.other_sequential_1.location, category='vertical', display_name='Other Subsection 1'
        )
        self.other_vertical_2 = ItemFactory.create(
            parent_location=self.other_sequential_2.location, category='vertical', display_name='Other Subsection 2'
        )

        # self.other_vertical_1 has two parents
        self.other_sequential_2.children.append(self.other_vertical_1.location)
        modulestore().update_item(self.other_sequential_2, self.admin.id)  # pylint: disable=no-member

        self.other_bookmark_1 = BookmarkFactory.create(
            user=self.user,
            course_key=unicode(self.other_course.id),
            usage_key=self.other_vertical_1.location,
            xblock_cache__display_name=self.other_vertical_1.display_name
        )

    def create_course_with_bookmarks_count(self, count, store_type=ModuleStoreEnum.Type.mongo):
        """
        Create a course, add some content and add bookmarks.
        """
        with self.store.default_store(store_type):

            course = CourseFactory.create()

            with self.store.bulk_operations(course.id):
                blocks = [ItemFactory.create(
                    parent_location=course.location, category='chapter', display_name=unicode(index)
                ) for index in range(count)]

            bookmarks = [BookmarkFactory.create(
                user=self.user,
                course_key=course.id,
                usage_key=block.location,
                xblock_cache__display_name=block.display_name
            ) for block in blocks]

        return course, blocks, bookmarks

    def assert_bookmark_model_is_valid(self, bookmark, bookmark_data):
        """
        Assert that the attributes of the bookmark model were set correctly.
        """
        self.assertEqual(bookmark.user, bookmark_data['user'])
        self.assertEqual(bookmark.course_key, bookmark_data['course_key'])
        self.assertEqual(unicode(bookmark.usage_key), unicode(bookmark_data['usage_key']))
        self.assertEqual(bookmark.resource_id, u"{},{}".format(bookmark_data['user'], bookmark_data['usage_key']))
        self.assertEqual(bookmark.display_name, bookmark_data['display_name'])
        self.assertEqual(bookmark.path, self.path)
        self.assertIsNotNone(bookmark.created)

        self.assertEqual(bookmark.xblock_cache.course_key, bookmark_data['course_key'])
        self.assertEqual(bookmark.xblock_cache.display_name, bookmark_data['display_name'])

    def assert_bookmark_data_is_valid(self, bookmark, bookmark_data, check_optional_fields=False):
        """
        Assert that the bookmark data matches the data in the model.
        """
        self.assertEqual(bookmark_data['id'], bookmark.resource_id)
        self.assertEqual(bookmark_data['course_id'], unicode(bookmark.course_key))
        self.assertEqual(bookmark_data['usage_id'], unicode(bookmark.usage_key))
        self.assertEqual(bookmark_data['block_type'], unicode(bookmark.usage_key.block_type))
        self.assertIsNotNone(bookmark_data['created'])

        if check_optional_fields:
            self.assertEqual(bookmark_data['display_name'], bookmark.display_name)
            self.assertEqual(bookmark_data['path'], bookmark.path)


@ddt.ddt
class BookmarkModelTests(BookmarksTestsBase):
    """
    Test the Bookmark model.
    """
    def get_bookmark_data(self, block, user=None):
        """
        Returns bookmark data for testing.
        """
        return {
            'user': user or self.user,
            'usage_key': block.location,
            'course_key': block.location.course_key,
            'display_name': block.display_name,
        }

    def test_create_bookmark_success(self):
        """
        Tests creation of bookmark.
        """
        bookmark_data = self.get_bookmark_data(self.vertical_2)
        bookmark = Bookmark.create(bookmark_data)
        self.assert_bookmark_model_is_valid(bookmark, bookmark_data)

        bookmark_data_different_values = self.get_bookmark_data(self.vertical_2)
        bookmark_data_different_values['display_name'] = 'Introduction Video'
        bookmark2 = Bookmark.create(bookmark_data_different_values)
        # The bookmark object already created should have been returned without modifications.
        self.assertEqual(bookmark, bookmark2)
        self.assertEqual(bookmark.xblock_cache, bookmark2.xblock_cache)
        self.assert_bookmark_model_is_valid(bookmark2, bookmark_data)

        bookmark_data_different_user = self.get_bookmark_data(self.vertical_2)
        bookmark_data_different_user['user'] = UserFactory.create()
        bookmark3 = Bookmark.create(bookmark_data_different_user)
        self.assertNotEqual(bookmark, bookmark3)
        self.assert_bookmark_model_is_valid(bookmark3, bookmark_data_different_user)

    @ddt.data(
        (-30, [[PathItem(EXAMPLE_USAGE_KEY_1, '1')]], 1),
        (30, None, 2),
        (30, [], 2),
        (30, [[PathItem(EXAMPLE_USAGE_KEY_1, '1')]], 1),
        (30, [[PathItem(EXAMPLE_USAGE_KEY_1, '1')], [PathItem(EXAMPLE_USAGE_KEY_2, '2')]], 2),
    )
    @ddt.unpack
    @mock.patch('openedx.core.djangoapps.bookmarks.models.Bookmark.get_path')
    def test_path(self, seconds_delta, paths, get_path_call_count, mock_get_path):

        block_path = [PathItem(UsageKey.from_string(EXAMPLE_USAGE_KEY_1), '1')]
        mock_get_path.return_value = block_path

        bookmark_data = self.get_bookmark_data(self.vertical_2)
        bookmark = Bookmark.create(bookmark_data)
        self.assertIsNotNone(bookmark.xblock_cache)

        modification_datetime = datetime.datetime.now(pytz.utc) + datetime.timedelta(seconds=seconds_delta)
        with freeze_time(modification_datetime):
            bookmark.xblock_cache.paths = paths
            bookmark.xblock_cache.save()

        self.assertEqual(bookmark.path, block_path)
        self.assertEqual(mock_get_path.call_count, get_path_call_count)

    @ddt.data(
        ('course', []),
        ('chapter_1', []),
        ('sequential_1', ['chapter_1']),
        ('vertical_1', ['chapter_1', 'sequential_1']),
        ('other_vertical_1', ['other_chapter_1', 'other_sequential_2']),  # Has two ancestors
    )
    @ddt.unpack
    def test_get_path(self, block_to_bookmark, ancestors_attrs):

        user = UserFactory.create()

        expected_path = [PathItem(
            usage_key=getattr(self, ancestor_attr).location, display_name=getattr(self, ancestor_attr).display_name
        ) for ancestor_attr in ancestors_attrs]

        bookmark_data = self.get_bookmark_data(getattr(self, block_to_bookmark), user=user)
        bookmark = Bookmark.create(bookmark_data)

        self.assertEqual(bookmark.path, expected_path)
        self.assertIsNotNone(bookmark.xblock_cache)
        self.assertEqual(bookmark.xblock_cache.paths, [])

    def test_get_path_in_case_of_exceptions(self):

        user = UserFactory.create()

        # Block does not exist
        usage_key = UsageKey.from_string('i4x://edX/apis/html/interactive')
        usage_key.replace(course_key=self.course.id)
        self.assertEqual(Bookmark.get_path(usage_key), [])

        # Block is an orphan
        self.other_sequential_2.children = []
        modulestore().update_item(self.other_sequential_2, self.admin.id)  # pylint: disable=no-member

        bookmark_data = self.get_bookmark_data(self.other_vertical_2, user=user)
        bookmark = Bookmark.create(bookmark_data)

        self.assertEqual(bookmark.path, [])
        self.assertIsNotNone(bookmark.xblock_cache)
        self.assertEqual(bookmark.xblock_cache.paths, [])

        # Parent block could not be retrieved
        with mock.patch('openedx.core.djangoapps.bookmarks.models.search.path_to_location') as mock_path_to_location:
            mock_path_to_location.return_value = [usage_key]
            bookmark_data = self.get_bookmark_data(self.other_sequential_1, user=user)
            bookmark = Bookmark.create(bookmark_data)
            self.assertEqual(bookmark.path, [])


@ddt.ddt
class XBlockCacheModelTest(ModuleStoreTestCase):
    """
    Test the XBlockCache model.
    """

    COURSE_KEY = CourseLocator(org='test', course='test', run='test')
    CHAPTER1_USAGE_KEY = BlockUsageLocator(COURSE_KEY, block_type='chapter', block_id='chapter1')
    SECTION1_USAGE_KEY = BlockUsageLocator(COURSE_KEY, block_type='section', block_id='section1')
    SECTION2_USAGE_KEY = BlockUsageLocator(COURSE_KEY, block_type='section', block_id='section1')
    VERTICAL1_USAGE_KEY = BlockUsageLocator(COURSE_KEY, block_type='vertical', block_id='sequential1')
    PATH1 = [
        {'usage_key': unicode(CHAPTER1_USAGE_KEY), 'display_name': 'Chapter 1'},
        {'usage_key': unicode(SECTION1_USAGE_KEY), 'display_name': 'Section 1'},
    ]
    PATH2 = [
        {'usage_key': unicode(CHAPTER1_USAGE_KEY), 'display_name': 'Chapter 1'},
        {'usage_key': unicode(SECTION2_USAGE_KEY), 'display_name': 'Section 2'},
    ]

    def setUp(self):
        super(XBlockCacheModelTest, self).setUp()

    def assert_xblock_cache_data(self, xblock_cache, data):
        """
        Assert that the XBlockCache object values match.
        """
        self.assertEqual(xblock_cache.usage_key, data['usage_key'])
        self.assertEqual(xblock_cache.course_key, data['usage_key'].course_key)
        self.assertEqual(xblock_cache.display_name, data['display_name'])

    @ddt.data(
        (
            [
                {'usage_key': VERTICAL1_USAGE_KEY, },
                {'display_name': '', 'paths': [], },
            ],
            [
                {'usage_key': VERTICAL1_USAGE_KEY, 'display_name': 'Vertical 5', 'paths': [PATH2]},
                {'paths': [PATH2]},
            ],
        ),
        (
            [
                {'usage_key': VERTICAL1_USAGE_KEY, 'display_name': 'Vertical 4', 'paths': [PATH1]},
                {},
            ],
            [
                {'usage_key': VERTICAL1_USAGE_KEY, 'display_name': 'Vertical 5', 'paths': [PATH2]},
                {'paths': [PATH1, PATH2]},
            ],
        ),
    )
    def test_create(self, data):
        """
        Test XBlockCache.create() constructs and updates objects correctly.
        """
        for create_data, additional_data_to_expect in data:
            xblock_cache = XBlockCache.create(create_data)
            create_data.update(additional_data_to_expect)
            self.assert_xblock_cache_data(xblock_cache, create_data)
