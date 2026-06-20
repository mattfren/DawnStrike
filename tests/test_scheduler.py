from intraday_scanner.scheduler import schedule_as_rows


def test_scheduler_pushes_real_recommendation_notifications():
    rows = schedule_as_rows()
    push = next(row for row in rows if row["name"] == "push-recommendations")
    monitor = next(row for row in rows if row["name"] == "monitor-open")

    assert push["time_ct"] == "08:15"
    assert push["command"].startswith("intraday-scan notify ")
    assert "notify-test" not in push["command"]
    assert "--provider alpaca" in monitor["command"]
    assert "--continuous" in monitor["command"]
