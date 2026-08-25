# Dwell-clean WIP checkpoint — 2026-08-26

This branch is a byte-for-byte backup checkpoint of the local construction workspace that existed before continuing P1 work.

- Upstream/base commit: `86240495d570ace933e17430ad86946d7df0a3a2`
- Original archive: `Dwell-clean-work_2026-08-26_0001_UTC+08.tar.gz`
- Original archive SHA-256: `6b65681bd0c8ee25139f6489f7b66ac2ced8510ad9ba8c3f7685e2d4e0b0950a`
- Archive size: about 170 KiB
- Stored here as four ordered Base64 chunks because the connector write path accepts text blobs.
- This checkpoint was not deployed and does not modify `main`.

## Restore

From the repository root on this branch:

```bash
cat checkpoint/2026-08-26/Dwell-clean-work.part-00.b64 \
    checkpoint/2026-08-26/Dwell-clean-work.part-01.b64 \
    checkpoint/2026-08-26/Dwell-clean-work.part-02.b64 \
    checkpoint/2026-08-26/Dwell-clean-work.part-03.b64 \
  | base64 -d > Dwell-clean-work_2026-08-26_0001_UTC+08.tar.gz

sha256sum Dwell-clean-work_2026-08-26_0001_UTC+08.tar.gz
# expected:
# 6b65681bd0c8ee25139f6489f7b66ac2ced8510ad9ba8c3f7685e2d4e0b0950a

tar -xzf Dwell-clean-work_2026-08-26_0001_UTC+08.tar.gz
```

The archive contains the local overlay modules, tests, `BASE_COMMIT`, and patches `0001` through `0007` from the WIP state.
