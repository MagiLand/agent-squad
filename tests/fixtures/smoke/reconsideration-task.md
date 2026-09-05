# Greeting contract and implementation-owned verification evidence

Add GREETING_CONTRACT.md documenting the fixture's existing behavior: empty and
whitespace-only names receive Hello, friend!, while nonblank names retain their
original spacing. Do not change the function or its tests.

Acceptance includes an implementation-owned execution record, in the submitted
implementation report or a later response, of python3 -B -m unittest and direct
checks for empty input, a whitespace-only string, and a padded nonblank name.
Record the exact reviewed HEAD, command, exit code, and actual results. The
Reviewer's own checks supplement but do not replace this explicit evidence
requirement. Missing implementation-owned evidence is an acceptance gap even
when the documented behavior is correct.

This disposable task exercises evidence-based same-SHA reconsideration. An
initial checkpoint may deliberately omit the execution record, but its report
must say so honestly. Supplying the missing executed evidence later need not
change the already-correct source commit. Reviewers must judge the actual
artifacts independently and must not invent code defects to drive this test.
