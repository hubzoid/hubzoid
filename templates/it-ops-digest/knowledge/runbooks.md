---
name: runbooks
description: One runbook section per alert name. The runbook tool reads this file by heading, so keep one "## AlertName" per alert.
keywords: [runbook, alerts, DiskFull, HighErrorRate, BackupFailed, CertExpiring]
---

# Runbooks (sample)

One section per alert name. The `runbook(alert_name)` tool returns the
section whose heading matches the name, case-insensitive. Keep steps
numbered and short. Replace these with your own.

## DiskFull

Severity: high. Fires when a filesystem passes 90 percent used.

1. Confirm on the host: `df -h` and note which mount.
2. If the mount is `/var/log`, rotate and compress: `logrotate -f /etc/logrotate.conf`.
3. If the mount holds the database, do not delete files. Page the database owner.
4. Check for a runaway process writing to the mount: `du -sh /var/* | sort -h`.
5. When below 80 percent, mark the incident resolved with the cause.

## HighErrorRate

Severity: high. Fires when 5xx responses exceed 2 percent over 5 minutes.

1. Check the deploy log for a release in the last 60 minutes.
2. If a release happened, roll back first, investigate second.
3. If no release, check upstream dependencies on the status page.
4. Capture a sample of failing requests before restarting anything.
5. Resolve when the rate is under 0.5 percent for 15 minutes.

## BackupFailed

Severity: medium. Fires when the nightly backup job exits non-zero.

1. Read the job log at `/var/log/backup/latest.log`.
2. If the failure is "no space", follow DiskFull on the backup host.
3. If the failure is a credential error, do not retry. Notify the IT-ops lead.
4. Otherwise re-run the job once by hand and watch it to completion.
5. A second failure in a row is escalated to the IT head the same morning.

## CertExpiring

Severity: low until 7 days out, then medium.

1. Note the host and the days remaining from the alert.
2. Renew through the certificate tool; do not renew by hand on the host.
3. Reload the service after renewal and confirm the new expiry.
4. Close the alert only after the check reports more than 30 days remaining.
