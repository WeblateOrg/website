# -*- coding: utf-8 -*-
#
# Copyright © 2012–2020 Michal Čihař <michal@cihar.com>
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

import argparse
import json
import os

import dateutil
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from weblate_web.models import Image, Post


class Command(BaseCommand):
    help = 'import blog posts'

    def add_arguments(self, parser):
        parser.add_argument(
            'json-file',
            type=argparse.FileType('r'),
            help='JSON file containing data to import',
        )

    def handle(self, *args, **options):
        data = json.load(options['json-file'])
        options['json-file'].close()
        missing = False

        # First validate we have all images in place
        for item in data:
            image = item['image_url'] or item['image']
            if not image:
                item['image_obj'] = None
            else:
                try:
                    item['image_obj'] = Image.objects.get(
                        name=os.path.basename(image)
                    )
                except Image.DoesNotExist:
                    self.stderr.write('Missing image: {}'.format(image))
                    missing = True
        if missing:
            raise CommandError('Missing image')

        # Actually import the data
        for item in data:
            with transaction.atomic():
                slug = item['slug']
                index = 0
                while Post.objects.filter(slug=slug).exists():
                    index += 1
                    slug = '{}_{}'.format(item['slug'], index)
                if slug != item['slug']:
                    self.stderr.write(
                        'Slug {} exists, using {} instead'.format(
                            item['slug'], slug
                        )
                    )
                Post.objects.create(
                    title=item['title'],
                    slug=slug,
                    timestamp=dateutil.parser.parse(item['date']),
                    body_markup_type=item['body_markup_type'],
                    body=item['body'],
                    summary=item['summary'],
                    image=item['image_obj'],
                )
