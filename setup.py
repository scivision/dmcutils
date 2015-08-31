#!/usr/bin/env python3

from setuptools import setup

with open('README.rst','r') as f:
	long_description = f.read()

setup(name='dmcutils',
      version='0.1',
	  description='Utilities for working with DMC (dual multi camera) Andor Neo sCMOS data',
	  long_description=long_description,
	  author='Michael Hirsch',
	  author_email='hirsch617@gmail.com',
	  url='https://github.com/scienceopen/dmcutils',
      dependency_links = ['https://github.com/scienceopen/histutils/tarball/master#egg=histutils',
                          'https://github.com/scienceopen/astrometry_azel/tarball/master#egg=astrometry_azel'],
	  install_requires=['histutils','astrometry_azel'],
      packages=['dmcutils'],
	  )

