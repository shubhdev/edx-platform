(function (define) {
    'use strict';

    define(['jquery',
            'underscore',
            'backbone',
            'js/components/tabbed/views/tabbed_view'
           ],
           function($, _, Backbone, TabbedView) {
               var view,
                   TestSubview = Backbone.View.extend({
                       initialize: function (options) {
                           this.text = options.text;
                       },

                       render: function () {
                           this.$el.text(this.text);
                       }
                   });

               beforeEach(function () {
                   view = new TabbedView({
                           tabs: [{
                               url: 'test 1',
                               title: 'Test 1',
                               view: new TestSubview({text: 'this is test text'})
                           }, {
                               url: 'test 2',
                               title: 'Test 2',
                               view: new TestSubview({text: 'other text'})
                           }]
                       });
               });

               describe('TabbedView component', function () {
                   it('can render itself', function () {
                       expect(view.$el.html()).toContain('<nav class="page-content-nav">')
                   });

                   it('shows its first tab by default', function () {
                       expect(view.$el.text()).toContain('this is test text');
                       expect(view.$el.text()).not.toContain('other text');
                   });

                   it('displays titles for each tab', function () {
                       expect(view.$el.text()).toContain('Test 1');
                       expect(view.$el.text()).toContain('Test 2');
                   });

                   it('can switch tabs', function () {
                       view.$('.nav-item[data-index=1]').click();
                       expect(view.$el.text()).not.toContain('this is test text');
                       expect(view.$el.text()).toContain('other text');
                   });
               });
           }
          );
}).call(this, define || RequireJS.define);
