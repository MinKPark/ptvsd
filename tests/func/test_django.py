# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See LICENSE in the project root
# for license information.

from __future__ import print_function, with_statement, absolute_import

import os.path
import pytest

import tests.helpers
from tests.helpers.pattern import ANY, Path
from tests.helpers.session import DebugSession
from tests.helpers.timeline import Event
from tests.helpers.pathutils import get_test_root
from tests.helpers.webhelper import get_url_from_str, get_web_content, wait_for_connection

DJANGO1_ROOT = get_test_root('django1')
DJANGO1_MANAGE = os.path.join(DJANGO1_ROOT, 'app.py')
DJANGO1_TEMPLATE = os.path.join(DJANGO1_ROOT, 'templates', 'hello.html')
DJANGO1_BAD_TEMPLATE = os.path.join(DJANGO1_ROOT, 'templates', 'bad.html')
DJANGO_PORT = tests.helpers.get_unique_port(8000)
DJANGO_LINK = 'http://127.0.0.1:{}/'.format(DJANGO_PORT)


@pytest.mark.parametrize('bp_target', ['code', 'template'])
@pytest.mark.parametrize('start_method', ['launch', 'attach_socket_cmdline'])
@pytest.mark.timeout(60)
def test_django_breakpoint_no_multiproc(bp_target, start_method):
    bp_file, bp_line, bp_name = {
        'code': (DJANGO1_MANAGE, 40, 'home'),
        'template': (DJANGO1_TEMPLATE, 8, 'Django Template'),
    }[bp_target]

    with DebugSession() as session:
        session.initialize(
            start_method=start_method,
            target=('file', DJANGO1_MANAGE),
            program_args=['runserver', '--noreload', '--', str(DJANGO_PORT)],
            debug_options=['Django'],
            cwd=DJANGO1_ROOT,
            expected_returncode=ANY.int,  # No clean way to kill Django server
        )

        bp_var_content = 'Django-Django-Test'
        session.set_breakpoints(bp_file, [bp_line])
        session.start_debugging()

        # wait for Django server to start
        wait_for_connection(DJANGO_PORT)
        web_request = get_web_content(DJANGO_LINK + 'home', {})

        hit = session.wait_for_thread_stopped()
        frames = hit.stacktrace.body['stackFrames']
        assert frames[0] == {
            'id': ANY.dap_id,
            'name': bp_name,
            'source': {
                'sourceReference': ANY,
                'path': Path(bp_file),
            },
            'line': bp_line,
            'column': 1,
        }

        fid = frames[0]['id']
        resp_scopes = session.send_request('scopes', arguments={
            'frameId': fid
        }).wait_for_response()
        scopes = resp_scopes.body['scopes']
        assert len(scopes) > 0

        resp_variables = session.send_request('variables', arguments={
            'variablesReference': scopes[0]['variablesReference']
        }).wait_for_response()
        variables = list(v for v in resp_variables.body['variables'] if v['name'] == 'content')
        assert variables == [{
                'name': 'content',
                'type': 'str',
                'value': repr(bp_var_content),
                'presentationHint': {'attributes': ['rawString']},
                'evaluateName': 'content',
                'variablesReference': 0,
            }]

        session.send_request('continue').wait_for_response(freeze=False)

        web_content = web_request.wait_for_response()
        assert web_content.find(bp_var_content) != -1

        # shutdown to web server
        link = DJANGO_LINK + 'exit'
        get_web_content(link).wait_for_response()

        session.wait_for_exit()


@pytest.mark.parametrize('start_method', ['launch', 'attach_socket_cmdline'])
@pytest.mark.timeout(60)
def test_django_template_exception_no_multiproc(start_method):
    with DebugSession() as session:
        session.initialize(
            start_method=start_method,
            target=('file', DJANGO1_MANAGE),
            program_args=['runserver', '--noreload', '--nothreading', str(DJANGO_PORT)],
            debug_options=['Django'],
            cwd=DJANGO1_ROOT,
            expected_returncode=ANY.int,  # No clean way to kill Django server
        )

        session.send_request('setExceptionBreakpoints', arguments={
            'filters': ['raised', 'uncaught'],
        }).wait_for_response()

        session.start_debugging()

        wait_for_connection(DJANGO_PORT)

        link = DJANGO_LINK + 'badtemplate'
        web_request = get_web_content(link, {})

        hit = session.wait_for_thread_stopped(reason='exception')
        frames = hit.stacktrace.body['stackFrames']
        assert frames[0] == ANY.dict_with({
            'id': ANY.dap_id,
            'name': 'Django TemplateSyntaxError',
            'source': ANY.dict_with({
                'sourceReference': ANY.dap_id,
                'path': Path(DJANGO1_BAD_TEMPLATE),
            }),
            'line': 8,
            'column': 1,
        })

        # Will stop once in the plugin
        resp_exception_info = session.send_request(
            'exceptionInfo',
            arguments={'threadId': hit.thread_id, }
        ).wait_for_response()
        exception = resp_exception_info.body
        assert exception == ANY.dict_with({
            'exceptionId': ANY.such_that(lambda s: s.endswith('TemplateSyntaxError')),
            'breakMode': 'always',
            'description': ANY.such_that(lambda s: s.find('doesnotexist') > -1),
            'details': ANY.dict_with({
                'message': ANY.such_that(lambda s: s.endswith('doesnotexist') > -1),
                'typeName': ANY.such_that(lambda s: s.endswith('TemplateSyntaxError')),
            })
        })

        session.send_request('continue').wait_for_response(freeze=False)

        # And a second time when the exception reaches the user code.
        hit = session.wait_for_thread_stopped(reason='exception')
        session.send_request('continue').wait_for_response(freeze=False)

        # ignore response for exception tests
        web_request.wait_for_response()

        # shutdown to web server
        link = DJANGO_LINK + 'exit'
        get_web_content(link).wait_for_response()

        session.wait_for_exit()


@pytest.mark.parametrize('ex_type', ['handled', 'unhandled'])
@pytest.mark.parametrize('start_method', ['launch', 'attach_socket_cmdline'])
@pytest.mark.timeout(60)
def test_django_exception_no_multiproc(ex_type, start_method):
    ex_line = {
        'handled': 50,
        'unhandled': 64,
    }[ex_type]

    with DebugSession() as session:
        session.initialize(
            start_method=start_method,
            target=('file', DJANGO1_MANAGE),
            program_args=['runserver', '--noreload', '--nothreading', str(DJANGO_PORT)],
            debug_options=['Django'],
            cwd=DJANGO1_ROOT,
            expected_returncode=ANY.int,  # No clean way to kill Django server
        )

        session.send_request('setExceptionBreakpoints', arguments={
            'filters': ['raised', 'uncaught'],
        }).wait_for_response()

        session.start_debugging()

        wait_for_connection(DJANGO_PORT)

        link = DJANGO_LINK + ex_type
        web_request = get_web_content(link, {})

        thread_stopped = session.wait_for_next(Event('stopped', ANY.dict_with({'reason': 'exception'})))
        assert thread_stopped == Event('stopped', ANY.dict_with({
            'reason': 'exception',
            'text': ANY.such_that(lambda s: s.endswith('ArithmeticError')),
            'description': 'Hello'
        }))

        tid = thread_stopped.body['threadId']
        resp_exception_info = session.send_request(
            'exceptionInfo',
            arguments={'threadId': tid, }
        ).wait_for_response()
        exception = resp_exception_info.body
        assert exception == {
            'exceptionId': ANY.such_that(lambda s: s.endswith('ArithmeticError')),
            'breakMode': 'always',
            'description': 'Hello',
            'details': {
                'message': 'Hello',
                'typeName': ANY.such_that(lambda s: s.endswith('ArithmeticError')),
                'source': Path(DJANGO1_MANAGE),
                'stackTrace': ANY.such_that(lambda s: True),
            }
        }

        resp_stacktrace = session.send_request('stackTrace', arguments={
            'threadId': tid,
        }).wait_for_response()
        assert resp_stacktrace.body['totalFrames'] > 1
        frames = resp_stacktrace.body['stackFrames']
        assert frames[0] == {
            'id': ANY.dap_id,
            'name': 'bad_route_' + ex_type,
            'source': {
                'sourceReference': ANY.dap_id,
                'path': Path(DJANGO1_MANAGE),
            },
            'line': ex_line,
            'column': 1,
        }

        session.send_request('continue').wait_for_response(freeze=False)

        # ignore response for exception tests
        web_request.wait_for_response()

        # shutdown to web server
        link = DJANGO_LINK + 'exit'
        get_web_content(link).wait_for_response()

        session.wait_for_exit()


@pytest.mark.skip()
@pytest.mark.timeout(120)
@pytest.mark.parametrize('start_method', ['launch'])
def test_django_breakpoint_multiproc(start_method):
    with DebugSession() as parent_session:
        parent_session.initialize(
            start_method=start_method,
            target=('file', DJANGO1_MANAGE),
            multiprocess=True,
            program_args=['runserver'],
            debug_options=['Django'],
            cwd=DJANGO1_ROOT,
            ignore_unobserved=[Event('stopped')],
            expected_returncode=ANY.int,  # No clean way to kill Django server
        )

        bp_line = 40
        bp_var_content = 'Django-Django-Test'
        parent_session.set_breakpoints(DJANGO1_MANAGE, [bp_line])
        parent_session.start_debugging()

        with parent_session.connect_to_next_child_session() as child_session:
            child_session.send_request('setBreakpoints', arguments={
                'source': {'path': DJANGO1_MANAGE},
                'breakpoints': [{'line': bp_line}, ],
            }).wait_for_response()
            child_session.start_debugging()

            # wait for Django server to start
            while True:
                child_session.proceed()
                o = child_session.wait_for_next(Event('output'))
                if get_url_from_str(o.body['output']) is not None:
                    break

            web_request = get_web_content(DJANGO_LINK + 'home', {})

            thread_stopped = child_session.wait_for_next(Event('stopped', ANY.dict_with({'reason': 'breakpoint'})))
            assert thread_stopped.body['threadId'] is not None

            tid = thread_stopped.body['threadId']

            resp_stacktrace = child_session.send_request('stackTrace', arguments={
                'threadId': tid,
            }).wait_for_response()
            assert resp_stacktrace.body['totalFrames'] > 0
            frames = resp_stacktrace.body['stackFrames']
            assert frames[0] == {
                'id': ANY.dap_id,
                'name': 'home',
                'source': {
                    'sourceReference': ANY.dap_id,
                    'path': Path(DJANGO1_MANAGE),
                },
                'line': bp_line,
                'column': 1,
            }

            fid = frames[0]['id']
            resp_scopes = child_session.send_request('scopes', arguments={
                'frameId': fid
            }).wait_for_response()
            scopes = resp_scopes.body['scopes']
            assert len(scopes) > 0

            resp_variables = child_session.send_request('variables', arguments={
                'variablesReference': scopes[0]['variablesReference']
            }).wait_for_response()
            variables = list(v for v in resp_variables.body['variables'] if v['name'] == 'content')
            assert variables == [{
                    'name': 'content',
                    'type': 'str',
                    'value': repr(bp_var_content),
                    'presentationHint': {'attributes': ['rawString']},
                    'evaluateName': 'content'
                }]

            child_session.send_request('continue').wait_for_response(freeze=False)

            web_content = web_request.wait_for_response()
            assert web_content.find(bp_var_content) != -1

            # shutdown to web server
            link = DJANGO_LINK + 'exit'
            get_web_content(link).wait_for_response()

            child_session.wait_for_termination()
            parent_session.wait_for_exit()
