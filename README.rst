====
zuup
====

.. image:: https://travis-ci.org/sileht/zuup.png?branch=master
   :target: https://travis-ci.org/sileht/zuup

.. image:: https://img.shields.io/pypi/v/zuup.svg
   :target: https://pypi.python.org/pypi/zuup/
   :alt: Latest Version

.. image:: https://img.shields.io/pypi/dm/zuup.svg
   :target: https://pypi.python.org/pypi/zuup/
   :alt: Downloads


Command line to consult Openstck zuul status

* Free software: Apache license
* Documentation: http://zuup.readthedocs.org
* Source: http://github.com/sileht/zuup
* Bugs: http://github.com/sileht/zuup/issues

Installation
------------

At the command line::

    $ pip install zuup

Or, if you have virtualenvwrapper installed::

    $ mkvirtualenv zuup
    $ pip install zuup

Usage
-----

To use zuup::

    $ zuup --help      
    usage: zuup [-h] [-D] [-d] [-w DELAY] [-e EXPIRATION] [-u USERNAME]
                [-p PROJECTS] [-c CHANGES] [-l] [-r] [-s] [-j JOB]

    optional arguments:
      -h, --help     show this help message and exit
      -D             Daemonize and exit if no more reviews
      -d             Daemonize
      -w DELAY       refresh delay
      -e EXPIRATION  review expiration in deamon mode
      -u USERNAME    Username
      -p PROJECTS    Projects
      -c CHANGES     changes
      -l             local changes
      -r             current repo changes
      -s             short output
      -j JOB         show log of a job of a change
