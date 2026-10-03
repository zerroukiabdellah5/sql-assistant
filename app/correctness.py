# ============================================================
# CORRECTNESS — STATED, NOT COMPUTED
# ============================================================
# This module publishes one fact on every successful query: nothing
# here checked whether the SQL answers the question. It exists
# because app/provenance.py describes a result thoroughly enough
# that a reader could mistake a described result for a checked one.
#
# PROVENANCE AND CORRECTNESS ARE INDEPENDENT
#   Provenance answers "where did these rows come from and how were
#   they produced". That is a set of observations about a statement
#   that ran. Correctness is a claim about a question that was
#   asked. The two are not degrees of the same thing: a query can be
#   perfectly traceable and still answer the wrong question, and a
#   query can be checked against an independent source and still be
#   untraceable here. Merging them into one block invites the reader
#   to take "we know exactly where these rows came from" as "we know
#   these rows are right". So they are separate keys, and this one
#   never grows a provenance field.
#
# WHAT WAS ACTUALLY CHECKED
#   The statement was parsed, checked against the deny list in
#   app/validate.py and compiled with EXPLAIN QUERY PLAN. That
#   establishes that it is a single read-only SELECT or WITH which
#   refers to things that exist. It says nothing about whether it
#   selects the right rows, the right columns, the right join, or
#   the right aggregate. Safety and correctness are separate, and
#   only safety is enforced here.
#
# WHY build_verification() TAKES NO ARGUMENTS
#   So that no caller can turn this into a claim by passing
#   something in. There is no flag, no confidence score and no
#   boolean a later change could flip to True: the block is
#   constant. If this application ever does check correctness, that
#   check gets its own module, its own response field and its own
#   tests, and this one keeps reporting that it did not run.
#
# WHY THERE IS NO "verified": false
#   A boolean named verified is one refactor away from True, and
#   clients read such a field as a status report rather than as the
#   absence of one. status is a word instead. "not_verified" names
#   what happened rather than what was achieved, and it has no true
#   state to drift into.
#
# WHAT IS NEVER PUT HERE
#   No rows, no SQL, no prompt, no schema, no database name, no
#   fingerprint, no model, no timestamp, no path and no session id.
#   Provenance holds those. This holds only the statement that they
#   were not checked, so a client cannot read a fact as a verdict.
#
# FAILURE IS NOT POSSIBLE
#   There is nothing to compute and nothing to read, so this cannot
#   raise, cannot depend on the filesystem, cannot time out and
#   cannot fail a query that already returned rows. It imports
#   nothing at all.
# ============================================================


# The only value status can take. A client may compare against it;
# there is no second value to branch on.
NOT_VERIFIED = "not_verified"

# One sentence, written for the person who asked the question rather
# than for a log reader. It names the two things a reader can act on:
# the query is model-written, and nothing confirmed the answer.
CORRECTNESS_NOTE = (
    "The query was written by a language model and ran read-only. "
    "Nothing here checked that it answers your question or that the "
    "rows are right: read the SQL, then confirm the numbers against "
    "a source you chose yourself."
)


def build_verification() -> dict:
    """The verification block attached to a successful /api/ask response.

    No parameters, no reads, no writes. Every call returns the same
    three facts, and there is no path through this function that
    returns anything else.
    """

    return {
        # What happened: no check was attempted. Not a score, and not
        # a claim that the answer is wrong.
        "status": NOT_VERIFIED,

        # False rather than null. "Unknown" would suggest the value
        # might become known later in this response; nothing else in
        # the response can change it.
        "correctness_checked": False,

        # The plain-language version of the two fields above, so a
        # client can display the fact instead of inferring it.
        "note": CORRECTNESS_NOTE,
    }
