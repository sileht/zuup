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
                                 'bashate', 'cookiecutter']}
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


def gerrit_query(query):
    cmd = ("ssh -x -p 29418 review.openstack.org "
           "'gerrit query %s --current-patch-set --format json'")
    cmd = cmd % query
    reviews = {}
    for line in command(cmd, 2):
        info = json.loads(line)
        if 'type' not in info:
            reviews[info["url"]] = info
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


class Zuup(object):
    def __init__(self):
        self.args = self.parse_args()

    @staticmethod
    def parse_args():
        parser = argparse.ArgumentParser()
        parser.add_argument('-D', dest='daemon_exit', action='store_true',
                            help="Daemonize and exit if no more reviews")
        parser.add_argument('-d', dest='daemon', action='store_false',
                            default=True,
                            help="Don't daemonize")
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
        parser.add_argument('-q', dest='queue', action='append',
                            help="queues", default=[])
        parser.add_argument('-l', dest='local', action='store_true',
                            help="local changes (deprecated and ignored)")
        parser.add_argument('-r', dest='repo', action='store_true',
                            help="current repo changes")
        parser.add_argument('-s', dest='short', action='store_true',
                            help="short output")
        parser.add_argument('-R', dest='running', action='store_true',
                            help="show only failed and running job")
        parser.add_argument('-j', dest='job',
                            help="show log of a job of a change")
        parser.add_argument('-n', dest='nonvoting', action="store_true",
                            help="show nonvoting job")

        return parser.parse_args()

    @staticmethod
    def normalize_changeid(change):
        return change.replace(
            "https://review.openstack.org/#/c/", '').replace(
                "https://review.openstack.org/", '').split('/')[0]

    @staticmethod
    def get_local_changeids():
        commits = command("git log --pretty=tformat:'%H' gerrit/master..HEAD")
        changeids = set()
        for commit in commits:
            changeid = command("git show %s | sed -n "
                               "'s/[[:space:]]*Change-Id: //p'" % commit,
                               min_lines=1)[0]
            changeids.add(changeid)
        return changeids

    @staticmethod
    def get_local_reponame():
        url = command("git config --local --get remote.gerrit.url")[0]
        return "/".join(url.split('/')[-2:]).replace('.git', '')

    def get_reviews_from_gerrit(self):
        reviews = {}

        if self.args.username:
            username = ' owner:%s' % self.args.username
        else:
            username = ''

        changes = set(self.args.changes)
        if not changes:
            changes.update(self.get_local_changeids())
        for change in changes:
            change = self.normalize_changeid(change)
            query = ' change:%s' % change
            reviews.update(gerrit_query(query))

        projects = set(self.args.projects)
        if self.args.repo:
            projects.add(self.get_local_reponame())

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

    def pretty_review(self, pipeline, zuul_review, review):
        remaining_time = zuul_review.get('remaining_time')
        enqueue_time = zuul_review.get('enqueue_time')

        output = ""
        if not self.args.short:
            output += "\n"
        output += "[%s] %s[%s]: %s" % (
            color(review['project'], 37, mod=1),
            color(pipeline['name'], 37),
            len(zuul_review.get('items_behind')),
            color(zuul_review.get('url'), 33),
        )
        if not self.args.short:
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
            url = self.get_log_url(review, job)

            voting_mod = None if bool(job.get('voting')) else 2
            if self.args.short:
                output += get_progress_bar_review(job)
            if not self.args.short or (self.args.short and
                                       job.get('result') == 'FAILURE'):
                if self.args.running and (job.get('result') == 'SUCCESS'
                                          or not url):
                    continue
                if not self.args.nonvoting and not bool(job.get('voting')):
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

    @staticmethod
    def get_log_url(review, job):
        if job.get('result') in ['SUCCESS', 'FAILURE']:
            info = dict(
                name=job["name"],
                pipeline=job["pipeline"],
                ref=review["currentPatchSet"]["ref"].replace(
                    "refs/changes/", ""),
                uuid=job["uuid"][:7],
            )
            return ('http://logs.openstack.org/%(ref)s'
                    '/%(pipeline)s/%(name)s/%(uuid)s' % info)
        else:
            url = job.get('url')
            if url:
                if url.startswith("stream.html"):
                    return "http://zuul.openstack.org/%s" % url
                else:
                    return url.replace('telnet://', '| socat - tcp:')
            else:
                return ''

    def get_zuul_reviews(self, gerrit_reviews):
        r = requests.get('http://zuul.openstack.org/api/status')
        if r.status_code != 200:
            raise UnexceptedException("Zuul request failed: \n%s" % r.text)

        data = r.json()
        for pipeline in data['pipelines']:
            #    print(pipeline['name'])
            if (self.args.queue and pipeline['name'] not in self.args.queue):
                continue
            for queue in pipeline['change_queues']:
                for zuul_reviews in queue['heads']:
                    for zuul_review in zuul_reviews:
                        if zuul_review['url'] in gerrit_reviews:
                            output = self.pretty_review(
                                pipeline, zuul_review,
                                gerrit_reviews[zuul_review['url']])
                            yield ((pipeline['name'], zuul_review['url']),
                                   (time.time(), output))

    def get_reviews(self):
        reviews = self.get_reviews_from_gerrit()
        return dict(self.get_zuul_reviews(reviews))

    def run(self):
        daemon = self.args.daemon or self.args.daemon_exit
        no_reviews_exit = self.args.daemon_exit or not self.args.daemon

        all_reviews = {}
        while True:
            try:
                new_reviews = self.get_reviews()
            except Exception as e:
                failure = True
                now = "fail: %s" % e
            else:
                failure = False
                now = str(datetime.datetime.now())[:-7]
                if self.args.expiration <= 0:
                    all_reviews = {}
                all_reviews.update(new_reviews)

            if self.args.expiration > 0:
                for url, (last_update, review) in list(all_reviews.items()):
                    if time.time() - last_update >= 60 * self.args.expiration:
                        del all_reviews[url]

            if not all_reviews:
                if daemon and not no_reviews_exit:
                    os.system('clear')
                if daemon:
                    print("")
                print(color("No reviews found in zuul", mod='1'))
                if failure:
                    print(now)
                if no_reviews_exit:
                    return
            elif daemon:
                os.system('clear')

            for data in all_reviews.values():
                print(data[1])

            if not daemon:
                break

            wait_input_or_timeout(
                self.args.delay, "\nLast update %s" % now, ", refreshing ...")


def main():
    try:
        Zuup().run()
    except KeyboardInterrupt:
        sys.stdout.write("\nExiting...\n")
        sys.stdout.flush()
