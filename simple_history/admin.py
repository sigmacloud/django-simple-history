from __future__ import unicode_literals

import datetime
from django import http
from django.core.exceptions import PermissionDenied
from django.conf.urls import url
from django.contrib import admin
from django.contrib.admin import helpers
from django.contrib.contenttypes.models import ContentType
from django.core.urlresolvers import reverse
from django.shortcuts import get_object_or_404, render
from django.utils.text import capfirst
from django.utils.html import mark_safe
from django.utils.translation import ugettext as _
from django.utils.encoding import force_text
from django.conf import settings
from django.contrib.admin.utils import unquote

try:
    from django.utils.version import get_complete_version
except ImportError:
    from django import VERSION
    get_complete_version = lambda: VERSION

USER_NATURAL_KEY = tuple(
    key.lower() for key in settings.AUTH_USER_MODEL.split('.', 1))

SIMPLE_HISTORY_EDIT = getattr(settings, 'SIMPLE_HISTORY_EDIT', False)


class SimpleHistoryAdmin(admin.ModelAdmin):
    object_history_template = "simple_history/object_history.html"
    object_history_form_template = "simple_history/object_history_form.html"

    def get_urls(self):
        """Returns the additional urls used by the Reversion admin."""
        urls = super(SimpleHistoryAdmin, self).get_urls()
        admin_site = self.admin_site
        opts = self.model._meta
        info = opts.app_label, opts.model_name
        history_urls = [
            url("^([^/]+)/history/([^/]+)/$",
                admin_site.admin_view(self.history_form_view),
                name='%s_%s_simple_history' % info),
        ]
        return history_urls + urls

    def history_view(self, request, object_id, extra_context=None):
        """The 'history' admin view for this model."""
        request.current_app = self.admin_site.name
        model = self.model
        opts = model._meta
        app_label = opts.app_label
        pk_name = opts.pk.attname
        history = getattr(model, model._meta.simple_history_manager_attribute)
        object_id = unquote(object_id)
        action_list = history.filter(**{pk_name: object_id})
        history_list_display = getattr(self, "history_list_display", [])
        # If no history was found, see whether this object even exists.
        try:
            obj = self.get_queryset(request).get(**{pk_name: object_id})
        except model.DoesNotExist:
            try:
                obj = action_list.latest('history_date').instance
            except action_list.model.DoesNotExist:
                raise http.Http404
        content_type = ContentType.objects.get_by_natural_key(
            *USER_NATURAL_KEY)
        admin_user_view = 'admin:%s_%s_change' % (content_type.app_label,
                                                  content_type.model)
        context = {
            'title': _('Change history: %s') % force_text(obj),
            'action_list': action_list,
            'module_name': capfirst(force_text(opts.verbose_name_plural)),
            'object': obj,
            'root_path': getattr(self.admin_site, 'root_path', None),
            'app_label': app_label,
            'opts': opts,
            'admin_user_view': admin_user_view,
            'history_list_display': history_list_display,
        }
        context.update(extra_context or {})
        extra_kwargs = {}
        if get_complete_version() < (1, 8):
            extra_kwargs['current_app'] = request.current_app
        return render(request, self.object_history_template, context, **extra_kwargs)

    def response_change(self, request, obj):
        if '_change_history' in request.POST and SIMPLE_HISTORY_EDIT:
            verbose_name = obj._meta.verbose_name

            msg = _('The %(name)s "%(obj)s" was changed successfully.') % {
                'name': force_text(verbose_name),
                'obj': force_text(obj)
            }

            self.message_user(
                request, "%s - %s" % (msg, _("You may edit it again below")))

            return http.HttpResponseRedirect(request.path)
        else:
            return super(SimpleHistoryAdmin, self).response_change(
                request, obj)

    def history_form_view(self, request, object_id, version_id):
        request.current_app = self.admin_site.name
        original_opts = self.model._meta
        model = getattr(
            self.model,
            self.model._meta.simple_history_manager_attribute).model
        historical_obj = get_object_or_404(model, **{
            original_opts.pk.attname: object_id,
            'history_id': version_id,
        })
        obj = historical_obj.instance
        obj._state.adding = False

        if not self.has_change_permission(request, obj):
            raise PermissionDenied

        if SIMPLE_HISTORY_EDIT:
            change_history = True
        else:
            change_history = False

        if '_change_history' in request.POST and SIMPLE_HISTORY_EDIT:
            obj = obj.history.get(pk=version_id).instance

        formsets = []
        form_class = self.get_form(request, obj)
        if request.method == 'POST':
            form = form_class(request.POST, request.FILES, instance=obj)
            if form.is_valid():
                new_object = self.save_form(request, form, change=True)
                self.save_model(request, new_object, form, change=True)
                form.save_m2m()

                self.log_change(request, new_object,
                                self.construct_change_message(
                                    request, form, formsets))
                return self.response_change(request, new_object)

        else:
            form = form_class(instance=obj)

        admin_form = helpers.AdminForm(
            form,
            self.get_fieldsets(request, obj),
            self.prepopulated_fields,
            self.get_readonly_fields(request, obj),
            model_admin=self,
        )

        model_name = original_opts.model_name
        url_triplet = self.admin_site.name, original_opts.app_label, model_name

        inline_instances = self.get_inline_instances(request, obj)
        prefixes = {}
        formset = []

        historical_date = historical_obj.history_date
        adjusted_historical_date = historical_date + datetime.timedelta(seconds=5)
        for FormSet, inline in self.get_admin_formsets_with_inline(
                *[request]):
            prefix = FormSet.get_default_prefix()
            prefixes[prefix] = prefixes.get(prefix, 0) + 1
            if prefixes[prefix] != 1 or not prefix:
                prefix = "%s-%s" % (prefix, prefixes[prefix])

            inline_qs = inline.get_queryset(request)
            inline_ids = inline_qs.values_list('id', flat=True)
            history_inline_model = inline_qs.first().history.model
            historical_ids = history_inline_model.objects\
                                    .filter(id__in=inline_ids, history_date__lte=adjusted_historical_date)\
                                    .order_by('-history_date')[:inline_qs.count()]\
                                    .values_list('history_id', flat=True)
            historical_queryset = history_inline_model.objects.filter(history_id__in=historical_ids)

            formset_params = {
                'instance': obj,
                'prefix': prefix,
                'queryset': historical_queryset,
            }
            if request.method == 'POST':
                formset_params.update({
                    'data': request.POST.copy(),
                    'files': request.FILES,
                    'save_as_new': '_saveasnew' in request.POST
                })
            formset.append(FormSet(**formset_params))

        inline_formsets = self.get_admin_inline_formsets(
            request, formset, inline_instances, obj)
        context = {
            'title': _('Revert %s') % force_text(obj),
            'adminform': admin_form,
            'object_id': object_id,
            'original': obj,
            'is_popup': False,
            'media': mark_safe(self.media + admin_form.media),
            'errors': helpers.AdminErrorList(form, formsets),
            'app_label': original_opts.app_label,
            'original_opts': original_opts,
            'changelist_url': reverse('%s:%s_%s_changelist' % url_triplet),
            'change_url': reverse('%s:%s_%s_change' % url_triplet,
                                  args=(obj.pk,)),
            'history_url': reverse('%s:%s_%s_history' % url_triplet,
                                   args=(obj.pk,)),
            'change_history': change_history,
            'inline_admin_formsets': inline_formsets,
            # Context variables copied from render_change_form
            'add': False,
            'change': True,
            'has_add_permission': self.has_add_permission(request),
            'has_change_permission': self.has_change_permission(request, obj),
            'has_delete_permission': self.has_delete_permission(request, obj),
            'has_file_field': True,
            'has_absolute_url': False,
            'form_url': '',
            'opts': model._meta,
            'content_type_id': ContentType.objects.get_for_model(
                self.model).id,
            'save_as': self.save_as,
            'save_on_top': self.save_on_top,
            'root_path': getattr(self.admin_site, 'root_path', None),
        }
        extra_kwargs = {}
        if get_complete_version() < (1, 8):
            extra_kwargs['current_app'] = request.current_app
        return render(request, self.object_history_form_template, context, **extra_kwargs)

    def get_admin_inline_formsets(self,
                                  request,
                                  formsets,
                                  inline_instances,
                                  obj=None):
        """ Django < 1.7 """
        inline_admin_formsets = []
        for inline, formset in zip(inline_instances, formsets):
            fieldsets = list(inline.get_fieldsets(request, obj))
            readonly = list(inline.get_fields(request, obj))  # list(inline.get_readonly_fields(request, obj))
            prepopulated = dict(inline.get_prepopulated_fields(request, obj))
            inline_admin_formset = helpers.InlineAdminFormSet(
                inline, formset, fieldsets, prepopulated, readonly,
                model_admin=self,
            )
            inline_admin_formsets.append(inline_admin_formset)
        return inline_admin_formsets

    def get_admin_formsets_with_inline(self, request, obj=None):
        """ Django < 1.7 """
        for inline in self.get_inline_instances(request, obj):
            yield inline.get_formset(request, obj), inline

    def save_model(self, request, obj, form, change):
        """Set special model attribute to user for reference after save"""
        obj._history_user = request.user
        super(SimpleHistoryAdmin, self).save_model(request, obj, form, change)
