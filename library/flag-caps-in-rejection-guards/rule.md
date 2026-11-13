---
id: flag-caps-in-rejection-guards
title: Flag upper caps written as rejection guards
proof: judged
---
# Flag upper caps written as rejection guards

A literal numeric upper limit must be caught whether it appears as a positive comparison, a reversed operand, or a negated guard such as `if not workers <= 3: raise`. Handle `assert`, `if ...: raise`, and `if ...: ... else: raise` forms, and keep the exemption for positive-count validation. Test each rewrite form before pushing.
