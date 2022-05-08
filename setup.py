from os import path
from setuptools import find_packages, setup

with open(path.join(path.dirname(__file__), 'README.md')) as readme:
    LONG_DESCRIPTION = readme.read()

setup(
    name='fava_portfolio_summary',
    use_scm_version=True,
    setup_requires=['setuptools_scm'],
    description='Fava extension to display portfolio summaries including MWRR and TWRR',
    long_description=LONG_DESCRIPTION,
    long_description_content_type='text/markdown',
    url='https://github.com/PhracturedBlue/fava-portfolio-summary',
    author='PhracturedBlue',
    author_email='rc2012@pblue.org',
    license='MIT',
    keywords='fava beancount accounting investment mwrr mwr twrr twr irr',
    packages=['fava_portfolio_summary'],
    package_dir={'fava_portfolio_summary': '.'},
    package_data={'': ['templates/PortfolioSummary.html']},
    include_package_data=True,
    install_requires=[
        'beancount>=2.3.4',
        'fava>=1.20',
    ],
    zip_safe=False,
    classifiers=[
        'Development Status :: 3 - Alpha',
        'Intended Audience :: Financial and Insurance Industry',
        'License :: OSI Approved :: MIT License',
        'Natural Language :: English',
        'Programming Language :: Python :: 3 :: Only',
        'Programming Language :: Python :: 3.7',
        'Topic :: Internet :: WWW/HTTP :: Dynamic Content',
        'Topic :: Office/Business :: Financial :: Accounting',
        'Topic :: Office/Business :: Financial :: Investment',
    ],
)
