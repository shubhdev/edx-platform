"""
Tests for bookmarks api.
"""
import ddt

from django.core.exceptions import ObjectDoesNotExist

from opaque_keys.edx.keys import UsageKey
from xmodule.modulestore.exceptions import ItemNotFoundError

from .. import api
from ..models import Bookmark
from .test_models import BookmarksTestsBase


@ddt.ddt
class BookmarksAPITests(BookmarksTestsBase):
    """
    These tests cover the parts of the API methods.
    """

    def setUp(self):
        super(BookmarksAPITests, self).setUp()

    def test_get_bookmark(self):
        """
        Verifies that get_bookmark returns data as expected.
        """
        bookmark_data = api.get_bookmark(user=self.user, usage_key=self.sequential_1.location)
        self.assert_bookmark_data_is_valid(self.bookmark_1, bookmark_data)

        # With Optional fields.
        with self.assertNumQueries(1):
            bookmark_data = api.get_bookmark(
                user=self.user,
                usage_key=self.sequential_1.location,
                fields=self.ALL_FIELDS
            )
        self.assert_bookmark_data_is_valid(self.bookmark_1, bookmark_data, check_optional_fields=True)

    def test_get_bookmark_raises_error(self):
        """
        Verifies that get_bookmark raises error as expected.
        """
        with self.assertNumQueries(1):
            with self.assertRaises(ObjectDoesNotExist):
                api.get_bookmark(user=self.other_user, usage_key=self.vertical_1.location)

    @ddt.data(
        1, 10, 100
    )
    def test_get_bookmarks(self, count):
        """
        Verifies that get_bookmarks returns data as expected.
        """
        course, __, bookmarks = self.create_course_with_bookmarks_count(count)

        # Without course key.
        with self.assertNumQueries(1):
            bookmarks_data = api.get_bookmarks(user=self.user)
            self.assertEqual(len(bookmarks_data), count + 3)
        # Assert them in ordered manner.
        self.assert_bookmark_data_is_valid(bookmarks[-1], bookmarks_data[0])
        self.assert_bookmark_data_is_valid(self.bookmark_1, bookmarks_data[-1])
        self.assert_bookmark_data_is_valid(self.bookmark_2, bookmarks_data[-2])

        # Without course key, with optional fields.
        with self.assertNumQueries(1):
            bookmarks_data = api.get_bookmarks(user=self.user, fields=self.ALL_FIELDS)
            self.assertEqual(len(bookmarks_data), count + 3)
        self.assert_bookmark_data_is_valid(bookmarks[-1], bookmarks_data[0])
        self.assert_bookmark_data_is_valid(self.bookmark_1, bookmarks_data[-1])

        # With course key.
        with self.assertNumQueries(1):
            bookmarks_data = api.get_bookmarks(user=self.user, course_key=course.id)
            self.assertEqual(len(bookmarks_data), count)
        self.assert_bookmark_data_is_valid(bookmarks[-1], bookmarks_data[0])
        self.assert_bookmark_data_is_valid(bookmarks[0], bookmarks_data[-1])

        # With course key, with optional fields.
        with self.assertNumQueries(1):
            bookmarks_data = api.get_bookmarks(user=self.user, course_key=course.id, fields=self.ALL_FIELDS)
            self.assertEqual(len(bookmarks_data), count)
        self.assert_bookmark_data_is_valid(bookmarks[-1], bookmarks_data[0])
        self.assert_bookmark_data_is_valid(bookmarks[0], bookmarks_data[-1])

        # Without Serialized.
        with self.assertNumQueries(1):
            bookmarks = api.get_bookmarks(user=self.user, course_key=course.id, serialized=False)
            self.assertEqual(len(bookmarks), count)
        self.assertTrue(bookmarks.model is Bookmark)  # pylint: disable=no-member

    def test_create_bookmark(self):
        """
        Verifies that create_bookmark create & returns data as expected.
        """
        self.assertEqual(len(api.get_bookmarks(user=self.user, course_key=self.course.id)), 2)

        with self.assertNumQueries(4):
            api.create_bookmark(user=self.user, usage_key=self.vertical_2.location)

        self.assertEqual(len(api.get_bookmarks(user=self.user, course_key=self.course.id)), 3)

    def test_create_bookmark_do_not_create_duplicates(self):
        """
        Verifies that create_bookmark do not create duplicate bookmarks.
        """
        self.assertEqual(len(api.get_bookmarks(user=self.user, course_key=self.course.id)), 2)

        with self.assertNumQueries(4):
            bookmark_data = api.create_bookmark(user=self.user, usage_key=self.vertical_2.location)

        self.assertEqual(len(api.get_bookmarks(user=self.user, course_key=self.course.id)), 3)

        with self.assertNumQueries(4):
            bookmark_data_2 = api.create_bookmark(user=self.user, usage_key=self.vertical_2.location)

        self.assertEqual(len(api.get_bookmarks(user=self.user, course_key=self.course.id)), 3)
        self.assertEqual(bookmark_data, bookmark_data_2)

    def test_create_bookmark_raises_error(self):
        """
        Verifies that create_bookmark raises error as expected.
        """
        with self.assertNumQueries(0):
            with self.assertRaises(ItemNotFoundError):
                api.create_bookmark(user=self.user, usage_key=UsageKey.from_string('i4x://brb/100/html/340ef1771a0940'))

    def test_delete_bookmark(self):
        """
        Verifies that delete_bookmark removes bookmark as expected.
        """
        self.assertEqual(len(api.get_bookmarks(user=self.user)), 3)

        with self.assertNumQueries(2):
            api.delete_bookmark(user=self.user, usage_key=self.sequential_1.location)

        bookmarks_data = api.get_bookmarks(user=self.user)
        self.assertEqual(len(bookmarks_data), 2)
        self.assertNotEqual(unicode(self.sequential_1.location), bookmarks_data[0]['usage_id'])
        self.assertNotEqual(unicode(self.sequential_1.location), bookmarks_data[1]['usage_id'])

    def test_delete_bookmark_raises_error(self):
        """
        Verifies that delete_bookmark raises error as expected.
        """
        with self.assertNumQueries(1):
            with self.assertRaises(ObjectDoesNotExist):
                api.delete_bookmark(user=self.other_user, usage_key=self.vertical_1.location)
