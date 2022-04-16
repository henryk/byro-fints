import os
from distutils.command.build import build

from django.core import management
from setuptools import find_packages, setup

try:
    with open(os.path.join(os.path.dirname(__file__), 'README.rst'), encoding='utf-8') as f:
        long_description = f.read()
except:
    long_description = ''


class CustomBuild(build):
    def run(self):
        management.call_command('compilemessages', verbosity=1)
        build.run(self)


cmdclass = {
    'build': CustomBuild
}


setup(
    name='byro-fints',
    version='0.0.5',
    description='Byro plugin to retrieve bank statements via FinTS 3.0 (formerly known as HBCI)',
    long_description=long_description,
    url='https://github.com/henryk/byro-fints',
    author='Henryk Plötz',
    author_email='henryk@ploetzli.ch',
    license='Apache Software License',
    install_requires=['fints==3.1.*', 'schwifty', 'django-securebox', 'django-enumfields==2.1.*'],
    packages=find_packages(exclude=['tests', 'tests.*']),
    include_package_data=True,
    cmdclass=cmdclass,
    entry_points="""
[byro.plugin]
byro_fints=byro_fints:ByroPluginMeta
""",
)
