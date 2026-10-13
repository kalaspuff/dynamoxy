# 0007: Repositories (data mapper) instead of active record

Status: Accepted

## Context

ORMs either put persistence on the model (`user.save()`, active record) or in a separate object
(data mapper / repository). DynamoDB access depends on a client (credentials, table prefixes,
middleware, hooks) and on key design that changes with access patterns.

## Decision

* Models are plain data with validation; they hold no client.
* `Repository[M]` (from `client.repository(M)` or subclasses) performs persistence; transactions
  plan model writes through the same repository code paths.

## Consequences

* Several clients (accounts, regions, test stores) can use the same models.
* Queries collect in repository subclasses, which keeps services independent of key design and
  easy to test.
* Slightly more ceremony than `user.save()`; the guide shows the pattern.
