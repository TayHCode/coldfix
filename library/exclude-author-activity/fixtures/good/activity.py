def last_maintainer_activity(events, author):
    return max(event["at"] for event in events if event["login"] != author)
