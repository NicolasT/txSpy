#!/usr/bin/env python

# txSpy, a set of tools to spy inside Twisted applications
#
# Copyright (C) 2009 Nicolas Trangez  <eikke eikke com>
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation, version 2.1
# of the License.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
# MA  02110-1301  USA

from distutils.core import setup

import txspy

setup(name='txSpy',
      version='.'.join(map(str, txspy.__version__)),
      description='A set of tools to spy inside Twisted applications', 
      author='Nicolas Trangez',
      author_email='eikke eikke com',
      packages=['txspy', ],
      license='LGPL-2.1',
      requires=['pygooglechart', 'twisted (>8.0)', ],
      url='http://github.com/NicolasT/txSpy',
     )
