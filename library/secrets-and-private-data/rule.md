---
id: secrets-and-private-data
title: Keep secrets and private data out of everything you write or publish
proof: rerunnable
scope: public
category: security
rank: 3
featured: true
summary: Scan tracked files for credentials before publishing. Redact personal data from logs, fixtures and free-text fields before those values reach the repository.
---
# Keep secrets and private data out of everything you write or publish

Anything written to logs, errors, evidence files, fixtures, docs or receipts must pass redaction: real people's names, phone numbers in any format (including compact forms), emails, tokens, raw provider ids, and hidden state. Detectors must be case-insensitive and cover free-form metadata. Treat free-text fields as untrusted. Run a secret and private-data scan before any push to a public repository, and never change visibility or publish without a passing scan and the repository owner's explicit approval for that release.

## Applies when

`docs/`, evidence files, fixtures, logging, error messages, redaction or sanitizing code, API responses that include stored JSON, and any push, release or visibility change.

## Reproduce

Commit a fake token and a term from your private denylist to a tracked doc, then run the scan: it reports both. A merge or visibility change that skips the scan lets them through.

## Fix

Run the scan as a required CI check on every pull request in repositories that are public or have a public mirror. Redact queries, titles and approval reasons before writing published notes. Gate pushes to public remotes and visibility changes on a passing scan of the current tree plus a recorded owner approval.

## Verify

In a throwaway repository, seed a fake token and a denylisted term: the scan must FAIL. Remove them: it must PASS. The publication gate must refuse without both a passing report for the current tree and an owner approval record.

## Encode it higher

A required scan CI job plus a pre-publication gate.
