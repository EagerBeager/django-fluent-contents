"""
Internal module for the plugin system,
the API is exposed via __init__.py
"""
from django.conf import settings
from django import forms
from django.contrib import admin
from django.contrib.contenttypes.models import ContentType
from django.contrib.sites.models import Site
from django.core import context_processors
from django.contrib.auth import context_processors as auth_context_processors
from django.contrib.messages import context_processors as messages_context_processors
from django.core.cache import cache
from django.db import DatabaseError
from django.template.context import Context
from django.template.loader import render_to_string
from django.utils.html import linebreaks, escape
from django.utils.translation import ugettext as _
from fluent_contents.cache import get_rendering_cache_key
from fluent_contents.forms import ContentItemForm


# Some standard request processors to use in the plugins,
# Naturally, you want STATIC_URL to be available in plugins.

def _add_debug(request):
    return {'debug': settings.DEBUG}

_STANDARD_REQUEST_CONTEXT_PROCESSORS = (
    context_processors.request,
    context_processors.static,
    context_processors.csrf,
    context_processors.media,
    context_processors.i18n,
    auth_context_processors.auth,
    messages_context_processors.messages,
    _add_debug,
)


class PluginContext(Context):
    """
    A template Context class similar to :class:`~django.template.context.RequestContext`, that enters some pre-filled data.
    This ensures that variables such as ``STATIC_URL`` and ``request`` are available in the plugin templates.
    """
    def __init__(self, request, dict=None, current_app=None):
        # If there is any reason to site-global context processors for plugins,
        # I'd like to know the usecase, and it could be implemented here.
        Context.__init__(self, dict, current_app=current_app)
        for processor in _STANDARD_REQUEST_CONTEXT_PROCESSORS:
            self.update(processor(request))


class ContentPlugin(object):
    """
    The base class for all content plugins.

    A plugin defines the rendering for a :class:`~fluent_contents.models.ContentItem`, settings and presentation in the admin interface.
    To create a new plugin, derive from this class and call :func:`plugin_pool.register <PluginPool.register>` to enable it.
    For example:

    .. code-block:: python

        from fluent_contents.extensions import plugin_pool, ContentPlugin

        @plugin_pool.register
        class AnnouncementBlockPlugin(ContentPlugin):
            model = AnnouncementBlockItem
            render_template = "plugins/announcementblock.html"
            category = _("Simple blocks")

    As minimal configuration, specify the :attr:`model` and :attr:`render_template` fields.
    The :attr:`model` should be a subclass of the :class:`~fluent_contents.models.ContentItem` model class.

    .. note::
        When the plugin is registered in the :attr:`plugin_pool`, it will be instantiated only once.
        It is therefore not possible to store per-request state at the plugin object.
        This is similar to the behavior of the :class:`~django.contrib.admin.ModelAdmin` classes in Django.

    To customize the admin, the :attr:`admin_form_template`, :attr:`admin_form` can be defined,
    and a ``class Media`` can be included to provide extra CSS and JavaScript files for the admin interface.
    Some well known properties of the :class:`~django.contrib.admin.ModelAdmin` class can also be specified on plugins;
    such as the
    :attr:`~django.contrib.admin.ModelAdmin.raw_id_fields`,
    :attr:`~django.contrib.admin.ModelAdmin.fieldsets` and
    :attr:`~django.contrib.admin.ModelAdmin.readonly_fields` settings.

    The rendered output of a plugin is cached by default, assuming that most content is static.
    This also avoids extra database queries to retrieve the model objects.
    In case the plugin needs to output content dynamically, include ``cache_output = False`` in the plugin definition.
    """
    __metaclass__ = forms.MediaDefiningClass

    # -- Settings to override:

    #: The model to use, must derive from :class:`fluent_contents.models.ContentItem`.
    model = None

    #: The form to use in the admin interface. By default it uses a  :class:`fluent_contents.models.ContentItemForm`.
    form = ContentItemForm

    #: The template to render the admin interface with
    admin_form_template = "admin/fluent_contents/contentitem/admin_form.html"

    #: An optional template which is included in the admin interface, to initialize components (e.g. JavaScript)
    admin_init_template = None

    #: The fieldsets for the admin view.
    fieldsets = None

    #: The template to render the frontend HTML output.
    render_template = None

    #: By default, rendered output is cached, and updated on admin changes.
    cache_output = True

    #: .. versionadded:: 0.9
    #: Cache the plugin output per :ref:`SITE_ID <site-id>`.
    cache_output_per_site = False

    #: The category to display the plugin at.
    category = None

    #: Alternative template for the view.
    ADMIN_TEMPLATE_WITHOUT_LABELS = "admin/fluent_contents/contentitem/admin_form_without_labels.html"

    #: .. versionadded:: 0.8.5
    #:    The ``HORIZONTAL`` constant for the :attr:`radio_fields`.
    HORIZONTAL = admin.HORIZONTAL

    #: .. versionadded:: 0.8.5
    #:    The ``VERTICAL`` constant for the :attr:`radio_fields`.
    VERTICAL = admin.VERTICAL

    #: The fields to display as raw ID
    raw_id_fields = ()

    #: The fields to display in a vertical filter
    filter_vertical = ()

    #: The fields to display in a horizontal filter
    filter_horizontal = ()

    #: The fields to display as radio choice. For example::
    #:
    #:    radio_fields = {
    #:        'align': ContentPlugin.VERTICAL,
    #:    }
    #:
    #: The value can be :attr:`ContentPlugin.HORIZONTAL` or :attr:`ContentPlugin.VERTICAL`.
    radio_fields = {}

    #: Fields to automatically populate with values
    prepopulated_fields = {}

    #: Overwritten formfield attributes, e.g. the 'widget'. Allows both the class and fieldname as key.
    formfield_overrides = {}

    #: The fields to display as readonly.
    readonly_fields = ()


    def __init__(self):
        self._type_id = None


    def __repr__(self):
        return '<{0} for {1} model>'.format(self.__class__.__name__, unicode(self.model.__name__).encode('ascii'))


    @property
    def verbose_name(self):
        """
        The title for the plugin, by default it reads the ``verbose_name`` of the model.
        """
        return self.model._meta.verbose_name


    @property
    def type_name(self):
        """
        Return the classname of the model, this is mainly provided for templates.
        """
        return self.model.__name__


    @property
    def type_id(self):
        """
        Shortcut to retrieving the ContentType id of the model.
        """
        if self._type_id is None:
            try:
                self._type_id = ContentType.objects.get_for_model(self.model).id
            except DatabaseError as e:
                raise DatabaseError("Unable to fetch ContentType object, is a plugin being registered before the initial syncdb? (original error: {0})".format(str(e)))

        return self._type_id


    def get_model_instances(self):
        """
        Return the model instances the plugin has created.
        """
        return self.model.objects.all()


    def _render_contentitem(self, request, instance):
        # Internal wrapper for render(), to allow updating the method signature easily.
        # It also happens to really simplify code navigation.
        return self.render(request=request, instance=instance)


    def get_output_cache_key(self, placeholder_name, instance):
        """
        .. versionadded:: 0.9
           Return the default cache key which is used to store a rendered item.
           By default, this function generates the cache key using :func:`~fluent_contents.cache.get_rendering_cache_key`.
        """
        cachekey = get_rendering_cache_key(placeholder_name, instance)
        if self.cache_output_per_site:
            cachekey = "{0}-s{1}".format(cachekey, settings.SITE_ID)
        return cachekey


    def get_output_cache_keys(self, placeholder_name, instance):
        """
        .. versionadded:: 0.9
           Return the possible cache keys for a rendered item.

           This method should be overwritten when implementing a function :func:`set_cached_output` method
           or implementing a :func:`get_output_cache_key` function which can return multiple results.
           By default, this function generates the cache key using :func:`get_output_cache_key`.
        """
        if self.cache_output_per_site:
            site_ids = list(Site.objects.values_list('pk', flat=True))
            if settings.SITE_ID not in site_ids:
                site_ids.append(settings.SITE_ID)

            base_key = get_rendering_cache_key(placeholder_name, instance)
            return ["{0}-s{1}".format(base_key, site_id) for site_id in site_ids]
        else:
            return [
                self.get_output_cache_key(placeholder_name, instance)
            ]


    def get_cached_output(self, placeholder_name, instance):
        """
        .. versionadded:: 0.9
           Return the cached output for a rendered item, or ``None`` if no output is cached.

           This method can be overwritten to implement custom caching mechanisms.
           By default, this function generates the cache key using :func:`get_output_cache_key`
           and retrieves the results from the configured Django cache backend (e.g. memcached).
        """
        cachekey = self.get_output_cache_key(placeholder_name, instance)
        return cache.get(cachekey)


    def set_cached_output(self, placeholder_name, instance, html):
        """
        .. versionadded:: 0.9
           Store the cached output for a rendered item.

           This method can be overwritten to implement custom caching mechanisms.
           By default, this function generates the cache key using :func:`~fluent_contents.cache.get_rendering_cache_key`
           and stores the results in the configured Django cache backend (e.g. memcached).

           When custom cache keys are used, also include those in :func:`get_output_cache_keys`
           so the cache will be cleared when needed.
        """
        cachekey = self.get_output_cache_key(placeholder_name, instance)
        cache.set(cachekey, html)


    def render(self, request, instance, **kwargs):
        """
        The rendering/view function that displays a plugin model instance.

        :param instance: An instance of the ``model`` the plugin uses.
        :param request: The Django :class:`~django.http.HttpRequest` class containing the request parameters.
        :param kwargs: An optional slot for any new parameters.

        To render a plugin, either override this function, or specify the :attr:`render_template` variable,
        and optionally override :func:`get_context`.
        It is recommended to wrap the output in a ``<div>`` tag,
        to prevent the item from being displayed right next to the previous plugin.

        To render raw HTML code, use :func:`~django.utils.safestring.mark_safe` on the returned HTML.
        """
        render_template = self.get_render_template(request, instance, **kwargs)
        if not render_template:
            return unicode(_(u"{No rendering defined for class '%s'}" % self.__class__.__name__))

        context = self.get_context(request, instance, **kwargs)
        return self.render_to_string(request, render_template, context)


    def render_to_string(self, request, template, context, content_instance=None):
        """
        Render a custom template with the :class:`~PluginContext` as context instance.
        """
        if not content_instance:
            content_instance = PluginContext(request)
        return render_to_string(template, context, context_instance=content_instance)


    def render_error(self, error):
        """
        A default implementation to render an exception.
        """
        return '<div style="color: red; border: 1px solid red; padding: 5px;">' \
               '<p><strong>%s</strong></p>%s</div>' % (_('Error:'), linebreaks(escape(str(error))))


    def get_render_template(self, request, instance, **kwargs):
        """
        Return the template to render for the specific model `instance` or `request`,
        By default it uses the ``render_template`` attribute.
        """
        return self.render_template


    def get_context(self, request, instance, **kwargs):
        """
        Return the context to use in the template defined by ``render_template`` (or :func:`get_render_template`).
        By default, it returns the model instance as ``instance`` field in the template.
        """
        return {
            'instance': instance,
        }