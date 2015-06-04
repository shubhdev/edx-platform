;(function (define) {
    'use strict';
    define(['backbone',
            'underscore',
            'jquery',
            'text!templates/components/tabbed/tabbed_view.underscore',
            'text!templates/components/tabbed/tab.underscore'],
           function (Backbone, _, $, tabbedViewTemplate, tabTemplate) {
               var TabbedView = Backbone.View.extend({
                   events: {
                       'click .nav-item': 'switchTab'
                   },

                   template: _.template(tabbedViewTemplate),

                   /**
                    * View for  a tabbed  interface. Expects a list of
                    * tabs in its options object, each of which should
                    * contain the following properties:
                      * view (Backbone.View): the view to render for this tab.
                      * title (string): The title to display for this tab.
                      * url (string): The URL fragment which will navigate to this tab.
                    */
                   initialize: function (options) {
                       this.router = new Backbone.Router();
                       this.$el.html(this.template({}));
                       var self = this;
                       this.tabs = _.map(options.tabs, function (viewObj, index) {
                           var tabEl = $(_.template(tabTemplate, {
                               index: index,
                               title: viewObj.title
                           }));
                           self.$('.page-content-nav').append(tabEl);

                           self.router.route(viewObj.url, function () {
                               self.setActiveTab(index);
                           });

                           return _.extend(viewObj, {tabEl: tabEl});
                       });
                       Backbone.history.start();
                       this.setActiveTab(0);
                   },

                   setActiveTab: function (index) {
                       this.$('a.is-active').removeClass('is-active');
                       this.tabs[index].tabEl.addClass('is-active');
                       var view = this.tabs[index].view;
                       view.render();
                       this.$('.page-content-main').html(view.$el.html());
                       this.router.navigate(this.tabs[index].url);
                   },

                   switchTab: function (event) {
                       event.preventDefault();
                       this.setActiveTab($(event.currentTarget).data('index'));
                   }
               });
               return TabbedView;
           });
}).call(this, define || RequireJS.define);
