"""
Tests for bookmark views.
"""

import ddt
import json
import urllib
from django.core.urlresolvers import reverse
from rest_framework.test import APIClient

from xmodule.modulestore import ModuleStoreEnum

from .test_models import BookmarksTestsBase


# pylint: disable=no-member
class BookmarksViewsTestsBase(BookmarksTestsBase):
    """
    Base class for bookmarks views tests.
    """
    STORE_TYPE = ModuleStoreEnum.Type.split

    def setUp(self):
        super(BookmarksViewsTestsBase, self).setUp()

        self.anonymous_client = APIClient()
        self.client = self.login_client(user=self.user)

    def login_client(self, user):
        """
        Helper method for getting the client and user and logging in. Returns client.
        """
        client = APIClient()
        client.login(username=user.username, password=self.TEST_PASSWORD)
        return client

    def send_get(self, client, url, query_parameters=None, expected_status=200):
        """
        Helper method for sending a GET to the server. Verifies the expected status and returns the response.
        """
        url = url + '?' + query_parameters if query_parameters else url
        response = client.get(url)
        self.assertEqual(expected_status, response.status_code)
        return response

    def send_post(self, client, url, data, content_type='application/json', expected_status=201):
        """
        Helper method for sending a POST to the server. Verifies the expected status and returns the response.
        """
        response = client.post(url, data=json.dumps(data), content_type=content_type)
        self.assertEqual(expected_status, response.status_code)
        return response

    def send_delete(self, client, url, expected_status=204):
        """
        Helper method for sending a DELETE to the server. Verifies the expected status and returns the response.
        """
        response = client.delete(url)
        self.assertEqual(expected_status, response.status_code)
        return response


@ddt.ddt
class BookmarksListViewTests(BookmarksViewsTestsBase):
    """
    This contains the tests for GET & POST methods of bookmark.views.BookmarksListView class
    GET /api/bookmarks/v0/bookmarks/?course_id={course_id1}
    POST /api/bookmarks/v0/bookmarks
    """
    @ddt.data(
        (1, False),
        (10, False),
        (100, False),
        (1, True),
        (10, True),
        (100, True),
    )
    @ddt.unpack
    def test_get_bookmarks_successfully(self, bookmarks_count, check_all_fields):
        """
        Test that requesting bookmarks for a course returns records successfully in
        expected order without optional fields.
        """

        course, __, bookmarks = self.create_course_with_bookmarks_count(
            bookmarks_count, store_type=ModuleStoreEnum.Type.mongo
        )

        query_parameters = 'course_id={}&page_size={}'.format(urllib.quote(unicode(course.id)), 100)
        if check_all_fields:
            query_parameters += '&fields=path,display_name'

        with self.assertNumQueries(7):  # 2 queries for bookmark table.
            response = self.send_get(
                client=self.client,
                url=reverse('bookmarks'),
                query_parameters=query_parameters,
            )

        bookmarks_data = response.data['results']

        self.assertEqual(len(bookmarks_data), len(bookmarks))
        self.assertEqual(response.data['count'], len(bookmarks))
        self.assertEqual(response.data['num_pages'], 1)

        # As bookmarks are sorted by -created so we will compare in that order.
        self.assert_bookmark_data_is_valid(bookmarks[-1], bookmarks_data[0], check_optional_fields=check_all_fields)
        self.assert_bookmark_data_is_valid(bookmarks[0], bookmarks_data[-1], check_optional_fields=check_all_fields)

    @ddt.data(
        10, 100
    )
    def test_get_bookmarks_with_pagination(self, bookmarks_count):
        """
        Test that requesting bookmarks for a course return results with pagination 200 code.
        """

        course, __, bookmarks = self.create_course_with_bookmarks_count(
            bookmarks_count, store_type=ModuleStoreEnum.Type.mongo
        )

        page_size = 5
        query_parameters = 'course_id={}&page_size={}'.format(urllib.quote(unicode(course.id)), page_size)

        with self.assertNumQueries(7):  # 2 queries for bookmark table.
            response = self.send_get(
                client=self.client,
                url=reverse('bookmarks'),
                query_parameters=query_parameters
            )

        bookmarks_data = response.data['results']

        # Pagination assertions.
        self.assertEqual(response.data['count'], bookmarks_count)
        self.assertIn('page=2&page_size={}'.format(page_size), response.data['next'])
        self.assertEqual(response.data['num_pages'], bookmarks_count / page_size)

        self.assertEqual(len(bookmarks_data), min(bookmarks_count, page_size))
        self.assert_bookmark_data_is_valid(bookmarks[-1], bookmarks_data[0])

    def test_get_bookmarks_with_invalid_data(self):
        """
        Test that requesting bookmarks with invalid data returns 0 records.
        """
        # Invalid course id.
        with self.assertNumQueries(5):  # No queries for bookmark table.
            response = self.send_get(
                client=self.client,
                url=reverse('bookmarks'),
                query_parameters='course_id=invalid'
            )
        bookmarks_data = response.data['results']
        self.assertEqual(len(bookmarks_data), 0)

    def test_get_all_bookmarks_when_course_id_not_given(self):
        """
        Test that requesting bookmarks returns all records for that user.
        """
        # Without course id we would return all the bookmarks for that user.

        with self.assertNumQueries(7):  # 2 queries for bookmark table.
            response = self.send_get(
                client=self.client,
                url=reverse('bookmarks')
            )
        bookmarks_data = response.data['results']
        self.assertEqual(len(bookmarks_data), 3)
        self.assert_bookmark_data_is_valid(self.other_bookmark_1, bookmarks_data[0])
        self.assert_bookmark_data_is_valid(self.bookmark_2, bookmarks_data[1])
        self.assert_bookmark_data_is_valid(self.bookmark_1, bookmarks_data[2])

    def test_anonymous_access(self):
        """
        Test that an anonymous client (not logged in) cannot call GET or POST.
        """
        query_parameters = 'course_id={}'.format(self.course_id)
        with self.assertNumQueries(1):  # No queries for bookmark table.
            self.send_get(
                client=self.anonymous_client,
                url=reverse('bookmarks'),
                query_parameters=query_parameters,
                expected_status=401
            )

        with self.assertNumQueries(1):  # No queries for bookmark table.
            self.send_post(
                client=self.anonymous_client,
                url=reverse('bookmarks'),
                data={'usage_id': 'test'},
                expected_status=401
            )

    def test_post_bookmark_successfully(self):
        """
        Test that posting a bookmark successfully returns newly created data with 201 code.
        """
        with self.assertNumQueries(9):
            response = self.send_post(
                client=self.client,
                url=reverse('bookmarks'),
                data={'usage_id': unicode(self.vertical_3.location)}
            )

        # Assert Newly created bookmark.
        self.assertEqual(response.data['id'], '%s,%s' % (self.user.username, unicode(self.vertical_3.location)))
        self.assertEqual(response.data['course_id'], self.course_id)
        self.assertEqual(response.data['usage_id'], unicode(self.vertical_3.location))
        self.assertIsNotNone(response.data['created'])
        self.assertEqual(len(response.data['path']), 2)
        self.assertEqual(response.data['display_name'], self.vertical_3.display_name)

    def test_post_bookmark_with_invalid_data(self):
        """
        Test that posting a bookmark for a block with invalid usage id returns a 400.
        Scenarios:
            1) Invalid usage id.
            2) Without usage id.
            3) With empty request.DATA
        """
        # Send usage_id with invalid format.
        with self.assertNumQueries(5):  # No queries for bookmark table.
            response = self.send_post(
                client=self.client,
                url=reverse('bookmarks'),
                data={'usage_id': 'invalid'},
                expected_status=400
            )
        self.assertEqual(response.data['user_message'], u'Invalid usage_id: invalid.')

        # Send data without usage_id.
        with self.assertNumQueries(4):  # No queries for bookmark table.
            response = self.send_post(
                client=self.client,
                url=reverse('bookmarks'),
                data={'course_id': 'invalid'},
                expected_status=400
            )
        self.assertEqual(response.data['user_message'], u'Parameter usage_id not provided.')
        self.assertEqual(response.data['developer_message'], u'Parameter usage_id not provided.')

        # Send empty data dictionary.
        with self.assertNumQueries(4):  # No queries for bookmark table.
            response = self.send_post(
                client=self.client,
                url=reverse('bookmarks'),
                data={},
                expected_status=400
            )
        self.assertEqual(response.data['user_message'], u'No data provided.')
        self.assertEqual(response.data['developer_message'], u'No data provided.')

    def test_post_bookmark_for_non_existing_block(self):
        """
        Test that posting a bookmark for a block that does not exist returns a 400.
        """
        with self.assertNumQueries(5):  # No queries for bookmark table.
            response = self.send_post(
                client=self.client,
                url=reverse('bookmarks'),
                data={'usage_id': 'i4x://arbi/100/html/340ef1771a094090ad260ec940d04a21'},
                expected_status=400
            )
        self.assertEqual(
            response.data['user_message'],
            u'Block with usage_id: i4x://arbi/100/html/340ef1771a094090ad260ec940d04a21 not found.'
        )
        self.assertEqual(
            response.data['developer_message'],
            u'Block with usage_id: i4x://arbi/100/html/340ef1771a094090ad260ec940d04a21 not found.'
        )

    def test_unsupported_methods(self):
        """
        Test that DELETE and PUT are not supported.
        """
        self.client.login(username=self.user.username, password=self.TEST_PASSWORD)
        self.assertEqual(405, self.client.put(reverse('bookmarks')).status_code)
        self.assertEqual(405, self.client.delete(reverse('bookmarks')).status_code)


@ddt.ddt
class BookmarksDetailViewTests(BookmarksViewsTestsBase):
    """
    This contains the tests for GET & DELETE methods of bookmark.views.BookmarksDetailView class
    """
    @ddt.data(
        ('', False),
        ('fields=path,display_name', True)
    )
    @ddt.unpack
    def test_get_bookmark_successfully(self, query_params, check_optional_fields):
        """
        Test that requesting bookmark returns data with 200 code.
        """
        with self.assertNumQueries(6):  # 1 query for bookmark table.
            response = self.send_get(
                client=self.client,
                url=reverse(
                    'bookmarks_detail',
                    kwargs={'username': self.user.username, 'usage_id': unicode(self.sequential_1.location)}
                ),
                query_parameters=query_params
            )
        data = response.data
        self.assertIsNotNone(data)
        self.assert_bookmark_data_is_valid(self.bookmark_1, data, check_optional_fields=check_optional_fields)

    def test_get_bookmark_that_belongs_to_other_user(self):
        """
        Test that requesting bookmark that belongs to other user returns 404 status code.
        """
        with self.assertNumQueries(5):  # No queries for bookmark table.
            self.send_get(
                client=self.client,
                url=reverse(
                    'bookmarks_detail',
                    kwargs={'username': 'other', 'usage_id': unicode(self.vertical_1.location)}
                ),
                expected_status=404
            )

    def test_get_bookmark_that_does_not_exist(self):
        """
        Test that requesting bookmark that does not exist returns 404 status code.
        """
        with self.assertNumQueries(6):  # 1 query for bookmark table.
            response = self.send_get(
                client=self.client,
                url=reverse(
                    'bookmarks_detail',
                    kwargs={'username': self.user.username, 'usage_id': 'i4x://arbi/100/html/340ef1771a0940'}
                ),
                expected_status=404
            )
        self.assertEqual(
            response.data['user_message'],
            'Bookmark with usage_id: i4x://arbi/100/html/340ef1771a0940 does not exist.'
        )
        self.assertEqual(
            response.data['developer_message'],
            'Bookmark with usage_id: i4x://arbi/100/html/340ef1771a0940 does not exist.'
        )

    def test_get_bookmark_with_invalid_usage_id(self):
        """
        Test that requesting bookmark with invalid usage id returns 400.
        """
        with self.assertNumQueries(5):  # No queries for bookmark table.
            response = self.send_get(
                client=self.client,
                url=reverse(
                    'bookmarks_detail',
                    kwargs={'username': self.user.username, 'usage_id': 'i4x'}
                ),
                expected_status=404
            )
        self.assertEqual(response.data['user_message'], u'Invalid usage_id: i4x.')

    def test_anonymous_access(self):
        """
        Test that an anonymous client (not logged in) cannot call GET or DELETE.
        """
        url = reverse('bookmarks_detail', kwargs={'username': self.user.username, 'usage_id': 'i4x'})
        with self.assertNumQueries(4):  # No queries for bookmark table.
            self.send_get(
                client=self.anonymous_client,
                url=url,
                expected_status=401
            )

        with self.assertNumQueries(1):
            self.send_delete(
                client=self.anonymous_client,
                url=url,
                expected_status=401
            )

    def test_delete_bookmark_successfully(self):
        """
        Test that delete bookmark returns 204 status code with success.
        """
        query_parameters = 'course_id={}'.format(urllib.quote(self.course_id))
        response = self.send_get(client=self.client, url=reverse('bookmarks'), query_parameters=query_parameters)
        bookmarks_data = response.data['results']
        self.assertEqual(len(bookmarks_data), 2)

        with self.assertNumQueries(6):  # 1 query for bookmark table.
            self.send_delete(
                client=self.client,
                url=reverse(
                    'bookmarks_detail',
                    kwargs={'username': self.user.username, 'usage_id': unicode(self.sequential_1.location)}
                )
            )
        response = self.send_get(client=self.client, url=reverse('bookmarks'), query_parameters=query_parameters)
        bookmarks_data = response.data['results']

        self.assertEqual(len(bookmarks_data), 1)

    def test_delete_bookmark_that_belongs_to_other_user(self):
        """
        Test that delete bookmark that belongs to other user returns 404.
        """
        with self.assertNumQueries(5):  # No queries for bookmark table.
            self.send_delete(
                client=self.client,
                url=reverse(
                    'bookmarks_detail',
                    kwargs={'username': 'other', 'usage_id': unicode(self.vertical_1.location)}
                ),
                expected_status=404
            )

    def test_delete_bookmark_that_does_not_exist(self):
        """
        Test that delete bookmark that does not exist returns 404.
        """
        with self.assertNumQueries(6):  # 1 query for bookmark table.
            response = self.send_delete(
                client=self.client,
                url=reverse(
                    'bookmarks_detail',
                    kwargs={'username': self.user.username, 'usage_id': 'i4x://arbi/100/html/340ef1771a0940'}
                ),
                expected_status=404
            )
        self.assertEqual(
            response.data['user_message'],
            u'Bookmark with usage_id: i4x://arbi/100/html/340ef1771a0940 does not exist.'
        )
        self.assertEqual(
            response.data['developer_message'],
            'Bookmark with usage_id: i4x://arbi/100/html/340ef1771a0940 does not exist.'
        )

    def test_delete_bookmark_with_invalid_usage_id(self):
        """
        Test that delete bookmark with invalid usage id returns 400.
        """
        with self.assertNumQueries(5):  # No queries for bookmark table.
            response = self.send_delete(
                client=self.client,
                url=reverse(
                    'bookmarks_detail',
                    kwargs={'username': self.user.username, 'usage_id': 'i4x'}
                ),
                expected_status=404
            )
        self.assertEqual(response.data['user_message'], u'Invalid usage_id: i4x.')

    def test_unsupported_methods(self):
        """
        Test that POST and PUT are not supported.
        """
        url = reverse('bookmarks_detail', kwargs={'username': self.user.username, 'usage_id': 'i4x'})
        self.client.login(username=self.user.username, password=self.TEST_PASSWORD)
        with self.assertNumQueries(5):  # No queries for bookmark table.
            self.assertEqual(405, self.client.put(url).status_code)

        with self.assertNumQueries(4):
            self.assertEqual(405, self.client.post(url).status_code)
