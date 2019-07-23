# -*- coding: utf-8 -*-
#
# Copyright © 2012–2019 Michal Čihař <michal@cihar.com>
#
# This file is part of Weblate <https://weblate.org/>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

from django.contrib import admin

from weblate_web.models import Donation, Image, Post, Reward


class RewardAdmin(admin.ModelAdmin):
    list_display = ('name', 'amount', 'recurring')
    list_filter = ('has_link', 'active', 'third_party')
    search_fields = ('name',)


class DonationAdmin(admin.ModelAdmin):
    list_display = ('user', 'reward_new', 'created', 'expires', 'get_amount')


class ImageAdmin(admin.ModelAdmin):
    search_fields = ('name',)


class PostAdmin(admin.ModelAdmin):
    list_display = ['title', 'timestamp', 'slug', 'image']
    list_filter = [('author', admin.RelatedOnlyFieldListFilter), 'topic']
    prepopulated_fields = {'slug': ('title',)}
    search_fields = ['title', 'slug']
    ordering = ('-timestamp',)
    date_hierarchy = 'timestamp'

    def save_model(self, request, obj, form, change):
        if getattr(obj, 'author', None) is None:
            obj.author = request.user
        obj.save()


admin.site.register(Image, ImageAdmin)
admin.site.register(Post, PostAdmin)
admin.site.register(Reward, RewardAdmin)
admin.site.register(Donation, DonationAdmin)
