# -*- encoding: utf-8 -*-
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import contextlib
import datetime
import json
import os
import signal
import subprocess
import sys
import termios
import time
import tty

import requests
import six


class UnexceptedException(Exception):
    pass


@contextlib.contextmanager
def raw_mode_and_hidden_output():
    fd = sys.stdin.fileno()
    tc_original = termios.tcgetattr(fd)
    tc_modified = termios.tcgetattr(fd)
    tc_modified[3] = tc_modified[3] & ~termios.ECHO
    termios.tcsetattr(fd, termios.TCSADRAIN, tc_modified)
    tty.setraw(fd)
    try:
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, tc_original)
        tty.setcbreak(fd)


@contextlib.contextmanager
def wait_until(timeout):
    class TimeoutExpired(Exception):
        pass

    def alarm_handler(signum, frame):
        raise TimeoutExpired

    signal.signal(signal.SIGALRM, alarm_handler)
    signal.alarm(timeout)
    try:
        yield
    except TimeoutExpired:
        pass
    finally:
        signal.alarm(0)


def wait_input_or_timeout(timeout, prompt1, prompt2):
    sys.stdout.write(prompt1)
    sys.stdout.flush()

    ret = None
    with raw_mode_and_hidden_output(), wait_until(timeout):
        if six.PY3:
            ret = sys.stdin.buffer.raw.read(1)
        else:
            ret = sys.stdin.read(1)

    if ret in [b'\x03', b'\x1b']:
        raise KeyboardInterrupt

    sys.stdout.write(prompt2)
    sys.stdout.flush()


def get_full_project(project):
    aliases = {'openstack-dev': ['devstack', 'hacking', 'grenade',
                                 'oslo-cookiecutter', 'pbr',
                                 'bashate', 'cookiecutter'],
               'stackforge': ['gnocchi', 'pecan', 'murano', 'solum', 'rally']}
    for namespace in aliases:
        if project in aliases[namespace]:
            return namespace + "/" + project
    if '/' not in project:
        return 'openstack/' + project
    return project


def command(cmd, min_lines=0):
    incoming = (sys.stdin.encoding or
                sys.getdefaultencoding())
    text = subprocess.check_output(cmd, shell=True)
    try:
        text = text.decode(incoming, 'strict')
    except UnicodeDecodeError:
        text = text.decode('utf-8', 'strict')
    lines = list(filter(lambda s: s,
                        map(lambda s: s.strip(),
                            text.split('\n'))))
    if len(lines) < min_lines:
        raise UnexceptedException("Invalid command returns: %s\n%s" %
                                  (cmd, text))
    return lines


def get_local_reponame():
    url = command("git config --local --get remote.gerrit.url")[0]
    return "/".join(url.split('/')[-2:]).replace('.git', '')


def get_local_changeids():
    commits = command("git log --pretty=tformat:'%H' gerrit/master..HEAD")
    changeids = set()
    for commit in commits:
        changeid = command("git show %s | "
                           "sed -n '/^Change-Id: / { s/.*: //;p;}'" % commit)
        changeids.add(changeid)
    return changeids


def gerrit_query(query):
    cmd = ("ssh -x -p 29418 review.openstack.org "
           "'gerrit query status:open %s --format json'")
    cmd = cmd % query
    reviews = {}
    for line in command(cmd, 2):
        info = json.loads(line)
        if 'type' not in info:
            reviews[info["url"]] = info
    return reviews


def get_gerrit_reviews(username, changes, projects):
    reviews = {}

    if username:
        username = ' owner:%s' % username
    else:
        username = ''

    for change in changes:
        query = ' change:%s' % change
        reviews.update(gerrit_query(query))

    for project in projects:
        project = get_full_project(project)
        project = ' project:%s' % project
        query = "%s %s" % (username, project)
        reviews.update(gerrit_query(query))
    else:
        if username:
            query = username
            reviews.update(gerrit_query(query))

    return reviews


def pretty_time(t, default="--:--:--", delta=False, finished=False):
    if t is not None and not finished:
        seconds = int(t) / 1000
        if not delta:
            seconds = (time.time() - seconds)
        t = "%s" % datetime.timedelta(seconds=seconds)
        t = t.split(".")[0]
        return t.rjust(8, '0')
    else:
        return default


def color(string, color=39, mod=0):
    attr = [str(color)]
    if mod:
        attr.append(str(mod))
    return '\x1b[%sm%s\x1b[0m' % (';'.join(attr), string)


def get_progress_bar_job(job):
    base = 7
    progress = '.' * base
    voting_mod = 1 if bool(job.get('voting')) else 2
    if not job['result'] and job.get('remaining_time', None) is not None:
            total_time = job['remaining_time'] + job['elapsed_time']
            remaining = job['remaining_time'] * base / total_time
            elapsed = job['elapsed_time'] * base / total_time
            progress = "%s%s" % ("=" * int(elapsed), "." * int(remaining))
            if len(progress) == base - 1:
                progress += '.'
    elif job.get('result') == 'SUCCESS':
        return color('SUCCESS', 32, voting_mod)
    elif job.get('result') == 'FAILURE':
        return color('FAILURE', 31, voting_mod)
    return color(progress, mod=voting_mod)


def get_progress_bar_review(job):
    voting_mod = 1 if bool(job.get('voting')) else 2
    if job.get('result') == 'SUCCESS':
        return color('S', 32, voting_mod)
    elif job.get('result') == 'FAILURE':
        return color('F', 31, voting_mod)
    else:
        return color('P', 33, voting_mod)


def get_log_url(zuul_review, job):
    if job.get('result') in ['SUCCESS', 'FAILURE']:
        if 'parameters' in job:
            return ('http://logs.openstack.org/%s' %
                    job['parameters']['LOG_PATH'])
        else:
            return job['url']
    return job.get('url', '') or ''


def pretty_review(pipeline, zuul_review, review, short_output=False,
                  running_output=False):
    remaining_time = zuul_review.get('remaining_time')
    enqueue_time = zuul_review.get('enqueue_time')

    output = ""
    if not short_output:
        output += "\n"
    output += "[%s] %s[%s]: %s" % (
        color(review['project'], 37, mod=1),
        color(pipeline['name'], 37),
        len(zuul_review.get('items_behind')),
        color(zuul_review.get('url'), 33),
    )
    if not short_output:
        output += "\n"
    output += " %s %s/%s " % (
        color(review['commitMessage'].split('\n')[0], 36),
        pretty_time(enqueue_time),
        pretty_time(remaining_time, delta=True),
    )

    details = ""
    jobs = zuul_review.get('jobs')
    for job in jobs:
        remaining_time = job.get('remaining_time')
        finished = job.get('result') in ['SUCCESS', 'FAILURE']
        url = get_log_url(zuul_review, job)

        voting_mod = None if bool(job.get('voting')) else 2
        if short_output:
            output += get_progress_bar_review(job)
        if not short_output or (short_output and
                                job.get('result') == 'FAILURE'):
            if (job.get('result') == 'SUCCESS' or not url) and running_output:
                continue
            details += "\n - %s %-8s %s %s" % (
                get_progress_bar_job(job),
                color(pretty_time(remaining_time, delta=True,
                                  finished=finished), mod=voting_mod),
                color(job['name'], mod=voting_mod),
                color(url, 33, mod=voting_mod)
            )

    output += details
    return output


def get_zuul_review(reviews, short_output=False, running_output=False):
    r = requests.get('http://zuul.openstack.org/status.json')
    if r.status_code != 200:
        raise UnexceptedException("Zuul request failed: \n%s" % r.text)

    data = r.json()
    for pipeline in data['pipelines']:
        #    print(pipeline['name'])
        for queue in pipeline['change_queues']:
            for zuul_reviews in queue['heads']:
                for zuul_review in zuul_reviews:
                    if zuul_review['url'] in reviews:
                        output = pretty_review(pipeline, zuul_review,
                                               reviews[zuul_review['url']],
                                               short_output, running_output)
                        yield ((pipeline['name'], zuul_review['url']),
                               (time.time(), output))


def normalize_changes(changes):
    for change in changes:
        yield change.replace(
            "https://review.openstack.org/#/c/", '').replace(
                "https://review.openstack.org/", '').split('/')[0]


def get_reviews(username, changes, projects, short_output, running_output):
    reviews = get_gerrit_reviews(username, changes, projects)
    return dict(get_zuul_review(reviews, short_output, running_output))


def zuup():
    parser = argparse.ArgumentParser()
    parser.add_argument('-D', dest='daemon_exit', action='store_true',
                        help="Daemonize and exit if no more reviews")
    parser.add_argument('-d', dest='daemon', action='store_true',
                        help="Daemonize")
    parser.add_argument('-w', dest='delay', default=60, type=int,
                        help="refresh delay")
    parser.add_argument('-e', dest='expiration', default=10, type=int,
                        help="review expiration in deamon mode")
    parser.add_argument('-u', dest='username',
                        help="Username")
    parser.add_argument('-p', dest='projects', action='append',
                        help="Projects", default=[])
    parser.add_argument('-c', dest='changes', action='append',
                        help="changes", default=[])
    parser.add_argument('-l', dest='local', action='store_true',
                        help="local changes", default=[])
    parser.add_argument('-r', dest='repo', action='store_true',
                        help="current repo changes", default=[])
    parser.add_argument('-s', dest='short', action='store_true',
                        help="short output")
    parser.add_argument('-R', dest='running', action='store_true',
                        help="show only failed and running job")
    parser.add_argument('-j', dest='job',
                        help="show log of a job of a change")

    args = parser.parse_args()

    daemon = args.daemon or args.daemon_exit
    no_reviews_exit = args.daemon_exit or not args.daemon

    changes = set(args.changes)
    if args.local:
        changes.update(get_local_changeids())
    changes = normalize_changes(changes)

    projects = set(args.projects)
    if args.repo:
        projects.add(get_local_reponame())

    all_reviews = {}
    while True:
        try:
            new_reviews = get_reviews(args.username, changes, projects,
                                      args.short, args.running)
        except Exception as e:
            now = "fail: %s" % e
        else:
            now = str(datetime.datetime.now())[:-7]
            if args.expiration <= 0:
                all_reviews = {}
            all_reviews.update(new_reviews)

        if args.expiration > 0:
            for url, (last_update, review) in list(all_reviews.items()):
                if time.time() - last_update >= 60 * args.expiration:
                    del all_reviews[url]

        if not all_reviews:
            if daemon and not no_reviews_exit:
                os.system('clear')
            if daemon:
                print()
            print(color("No reviews found in zuul", mod='1'))
            if no_reviews_exit:
                return
        elif daemon:
            os.system('clear')

        for data in all_reviews.values():
            print(data[1])

        if not daemon:
            break

        wait_input_or_timeout(
            args.delay, "\nLast update %s" % now, ", refreshing ...")


def main():
    try:
        zuup()
    except KeyboardInterrupt:
        sys.stdout.write("\nExiting...\n")
        sys.stdout.flush()
