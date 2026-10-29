from app.engine import safety

OK = "Hi Priya,\n\nThanks again for the demo. You asked about SSO; it is supported. Would Thursday work?\n\nBest,\nSam"


def check(
    body=OK,
    subject="Re: Product demo",
    corpus="Asked about SSO. Quoted $1,200.",
    recipient="p@acme.io",
):
    return safety.check(
        subject=subject,
        body=body,
        recipient=recipient,
        client_email="p@acme.io",
        thread_subject="Product demo",
        allowed_facts=[],
        fact_corpus=corpus,
    )


def test_clean_draft_passes():
    assert check() == []


def test_each_rule():
    assert "recipient" in check(recipient="x@evil.com")[0]
    assert any("placeholder" in f for f in check(OK.replace("Priya", "[Name]")))
    assert any("placeholder" in f for f in check(OK.replace("SSO", "{company}")))
    assert any("150" in f for f in check("word " * 151))
    assert any("subject" in f for f in check(subject="Quick question"))
    assert any("AI" in f for f in check(OK + "\n(This automated email was sent for Sam.)"))
    assert any("price" in f for f in check(OK.replace("Would", "It is $999. Would")))
    assert check(OK.replace("Would", "It is $1,200. Would")) == []
    assert any("date" in f for f in check(OK.replace("Would Thursday", "Shall we meet on March 3? Would")))
    assert any("offer" in f for f in check(OK.replace("Would", "I can offer a discount. Would")))
    assert any("banned opening" in f for f in check("Hi Priya,\n\nJust circling back on the demo."))
    assert any("banned phrase" in f for f in check(OK + " Don't hesitate to reach out."))


def test_extract_facts():
    assert safety.extract_facts("Quoted $1,200 for 40 seats on Oct 22, 15% off") == [
        "$1200",
        "15%",
        "oct22",
    ]
