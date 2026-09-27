"""Test runner that marks the process as a test run.

`IS_TEST_RUN` is the one signal startup checks may use to relax a
production guard (today: the Zenodo endpoint check in
`projects/checks.py`). It is set only by this runner, so `manage.py
runserver`, gunicorn, and management commands never see it.
"""

from django.test.runner import DiscoverRunner

IS_TEST_RUN = False


class OspreyTestRunner(DiscoverRunner):
    def setup_test_environment(self, **kwargs):
        global IS_TEST_RUN
        IS_TEST_RUN = True
        super().setup_test_environment(**kwargs)
