from intraday_scanner.config import ScannerConfig
from intraday_scanner.providers.sec_provider import SECRSSProvider, filing_has_dilution_risk


class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return b"""
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <title>S-3 - NOVA files shelf registration</title>
            <updated>2026-06-20T09:36:00-04:00</updated>
            <category term="S-3" />
            <link href="https://www.sec.gov/example" />
          </entry>
        </feed>
        """


def test_sec_rss_provider_parses_filings_without_network(monkeypatch):
    def fake_urlopen(request, timeout):
        return _FakeResponse()

    monkeypatch.setattr(
        "intraday_scanner.providers.sec_provider.urllib.request.urlopen",
        fake_urlopen,
    )

    provider = SECRSSProvider(ScannerConfig())
    filings = provider.get_filings(["NOVA"])

    assert len(filings) == 1
    assert filings[0].ticker == "NOVA"
    assert filings[0].filing_type == "S-3"
    assert filing_has_dilution_risk(filings[0])
