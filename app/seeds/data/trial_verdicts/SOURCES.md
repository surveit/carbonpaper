# Sources: five federal criminal jury trials, verdicts included

A mirror of 16 transcript files from CourtListener's RECAP archive, read by the `recap_files` connector through the `trial_verdicts` bundle's `cache_dir`, so a run never calls CourtListener. `manifest.jsonl` is the fetch record for every file here, with paths relative to this directory.

## Terms

RECAP archive file on storage.courtlistener.com, Free Law Project. ToS https://wiki.free.law/c/terms/courtlistener/courtlistenercom-terms-of-service-and-policies (modified 2026-08-05): filings are 'generally in the public domain' but may contain third-party copyrighted works; do not present the data so as to suggest Free Law Project produced, endorsed, or verified an AI-generated analysis.

## How the five were found

On 2026-11-11, CourtListener's RECAP search (www.courtlistener.com, in a browser; the search API now refuses anonymous calls) was run for transcript filings, available as PDFs, whose text holds "voir dire", "opening statement", "direct examination", "closing argument", "we, the jury" and "not guilty", and separately for "we the jury", "opening statement" and "closing argument". Each candidate's docket was then read for its other trial-day transcripts. A trial was kept when every day or volume of its trial, through the verdict, is in RECAP, the PDFs carry a text layer pypdf reads, the trial has one defendant, and the charge is not one that would put sexual-abuse material describing children into the repository.

Every trial kept ended in conviction on every count. A transcript reaches RECAP when someone buys it, mostly for an appeal, which a defendant brings only after a conviction.

## Files

| Trial | ECF | File | Pages | What it is | CourtListener URL | sha256 | Fetched at |
|---|---|---|---|---|---|---|---|
| United States v. Joos | 80 | `pdfs/mowd-3-09-cr-05022_ecf0080.pdf` | 134 | Criminal Jury Trial - Day 1, January 11, 2010 | https://storage.courtlistener.com/recap/gov.uscourts.mowd.91111/gov.uscourts.mowd.91111.80.0.pdf | `60b0df2d4e41f87c3564c3f922a4042a7ebf87a0c4040828f9dcb7d709563ca0` | 2026-11-11T17:33:32Z |
| United States v. Joos | 81 | `pdfs/mowd-3-09-cr-05022_ecf0081.pdf` | 135 | Criminal Jury Trial - Day 2, January 12, 2010 | https://storage.courtlistener.com/recap/gov.uscourts.mowd.91111/gov.uscourts.mowd.91111.81.0.pdf | `4251765b8eeb8225d7fecb05f62ce8d2ada747e9e144fb2fb015b585522a1b79` | 2026-11-11T17:33:33Z |
| United States v. Poke | 141 | `pdfs/ilnd-3-11-cr-50062_ecf0141.pdf` | 240 | Transcript of trial, Volume 1, May 6, 2013 | https://storage.courtlistener.com/recap/gov.uscourts.ilnd.259936/gov.uscourts.ilnd.259936.141.0.pdf | `4b8738da80ac45bb01989420c1ac5d201aa55fa3bc4d1b7c84d538ba9ecee922` | 2026-11-11T17:33:34Z |
| United States v. Poke | 142 | `pdfs/ilnd-3-11-cr-50062_ecf0142.pdf` | 231 | Transcript of trial, Volume 2, May 7, 2013 | https://storage.courtlistener.com/recap/gov.uscourts.ilnd.259936/gov.uscourts.ilnd.259936.142.0.pdf | `2ff5e167f2cf5ca27153e0129aa919de44815251cc7d961e85df4e3aab7f6818` | 2026-11-11T17:33:35Z |
| United States v. Atta | 114 | `pdfs/casd-3-21-cr-01289_ecf0114.pdf` | 195 | Jury Trial - Day One, September 26, 2022 | https://storage.courtlistener.com/recap/gov.uscourts.casd.706125/gov.uscourts.casd.706125.114.0.pdf | `1bf48fcbc1a6940d2ec1f2f63d5e3388b611fd61ece34ef7f538bc6946de9bf1` | 2026-11-11T17:33:37Z |
| United States v. Atta | 115 | `pdfs/casd-3-21-cr-01289_ecf0115.pdf` | 213 | Jury Trial - Day Two, September 27, 2022 | https://storage.courtlistener.com/recap/gov.uscourts.casd.706125/gov.uscourts.casd.706125.115.0.pdf | `383a40a297664184b496705d9bf73b83d69cd5f95a5e8ac677b979598f1a04d6` | 2026-11-11T17:33:38Z |
| United States v. Craft | 97 | `pdfs/kywd-3-22-cr-00094_ecf0097.pdf` | 54 | Jury Trial Vol. 1, March 6, 2023 (redacted) | https://storage.courtlistener.com/recap/gov.uscourts.kywd.127039/gov.uscourts.kywd.127039.97.0.pdf | `dad0ee27ef6006f2f853042ccdb8d741762ae9b34cd4928347e99452b2ada559` | 2026-11-11T17:33:39Z |
| United States v. Craft | 98 | `pdfs/kywd-3-22-cr-00094_ecf0098.pdf` | 237 | Jury Trial Vol. 2, March 7, 2023 (redacted) | https://storage.courtlistener.com/recap/gov.uscourts.kywd.127039/gov.uscourts.kywd.127039.98.0.pdf | `5777e9959e1955b5c9fb588dbe97bcd7bd1feef7d82872f4bb5b5e209ff3e5cb` | 2026-11-11T17:33:41Z |
| United States v. Craft | 99 | `pdfs/kywd-3-22-cr-00094_ecf0099.pdf` | 129 | Jury Trial Vol. 3, March 8, 2023 (redacted) | https://storage.courtlistener.com/recap/gov.uscourts.kywd.127039/gov.uscourts.kywd.127039.99.0.pdf | `ce909658f0cbca30a0c845090b7ac28190c4516a8159776050c73ab36490f532` | 2026-11-11T17:33:42Z |
| United States v. Craft | 100 | `pdfs/kywd-3-22-cr-00094_ecf0100.pdf` | 141 | Jury Trial Vol. 4, March 9, 2023 (redacted) | https://storage.courtlistener.com/recap/gov.uscourts.kywd.127039/gov.uscourts.kywd.127039.100.0.pdf | `acc21965cbb92a0f5f0e9186064813e57415f6f13a7e610271fd5c863729a3f0` | 2026-11-11T17:33:43Z |
| United States v. Craft | 101 | `pdfs/kywd-3-22-cr-00094_ecf0101.pdf` | 92 | Jury Trial Vol. 5, March 10, 2023 (redacted) | https://storage.courtlistener.com/recap/gov.uscourts.kywd.127039/gov.uscourts.kywd.127039.101.0.pdf | `967160569f3c04b5c113b95b451b2061bae43581a12b4c20a4a52dce01971276` | 2026-11-11T17:33:45Z |
| United States v. Tulali | 152 | `pdfs/akd-4-23-cr-00003_ecf0152.pdf` | 130 | Trial by Jury - Trial Day 1, April 15, 2024 | https://storage.courtlistener.com/recap/gov.uscourts.akd.70443/gov.uscourts.akd.70443.152.0.pdf | `5f1f181d3daacfab4097e924537364fb90ba013107a6cd79149be1ad654597c5` | 2026-11-11T17:33:46Z |
| United States v. Tulali | 153 | `pdfs/akd-4-23-cr-00003_ecf0153.pdf` | 226 | Trial by Jury - Trial Day 2, April 16, 2024 | https://storage.courtlistener.com/recap/gov.uscourts.akd.70443/gov.uscourts.akd.70443.153.0.pdf | `bba303c0e35f5b87ddac5aa177d194a9a5f3c7ccdc32e78a50b9202d0e93b8ab` | 2026-11-11T17:33:47Z |
| United States v. Tulali | 154 | `pdfs/akd-4-23-cr-00003_ecf0154.pdf` | 252 | Trial by Jury - Trial Day 3, April 17, 2024 | https://storage.courtlistener.com/recap/gov.uscourts.akd.70443/gov.uscourts.akd.70443.154.0.pdf | `f37db38fb1769c839da77a44504ddd448be3322eadd0c0f5c6546039094df788` | 2026-11-11T17:33:49Z |
| United States v. Tulali | 155 | `pdfs/akd-4-23-cr-00003_ecf0155.pdf` | 172 | Trial by Jury - Trial Day 4, April 18, 2024 | https://storage.courtlistener.com/recap/gov.uscourts.akd.70443/gov.uscourts.akd.70443.155.0.pdf | `c82d95c4f1257ef991fe3b853ead801e5a0dcd6c9d7d5c0ab5d6248e1f7a544c` | 2026-11-11T17:33:50Z |
| United States v. Tulali | 156 | `pdfs/akd-4-23-cr-00003_ecf0156.pdf` | 77 | Trial by Jury - Trial Day 5, April 19, 2024 | https://storage.courtlistener.com/recap/gov.uscourts.akd.70443/gov.uscourts.akd.70443.156.0.pdf | `d529de7c348980c58acf1a14faaee798b0196b9ad3e531bc069ecec36241ecf5` | 2026-11-11T17:33:51Z |

16 files, 2658 pages.

## Left out

| Case | Why |
|---|---|
| United States v. Dugan, S.D.W. Va. 2:21-cr-00127 (ECF 154) | Complete one-day trial; the charge concerns child sexual abuse material. |
| United States v. Rogers, N.D. Ohio 3:18-cr-00026 (ECF 92, 93) | Complete; the charge concerns child sexual abuse material. |
| United States v. Rosario Torres, M.D. Fla. 8:22-cr-00363 (ECF 232) | Two defendants, one acquitted on a count; the transcripts of March 5 (afternoon) and March 6, 2024 are not in RECAP. |
| United States v. De La Cruz-Alvarez, W.D. Tex. 3:25-mj-02401 (ECF 66) | The day 2 transcript ends with the jury sent home undecided; no transcript in RECAP holds the verdict. |
| State v. Novak, filed as Exhibit A in Novak v. City of Parma, N.D. Ohio 1:17-cv-02148 (ECF 1-1) | The PDF is a scan with no text layer pypdf reads. |
| United States v. Davis, S.D. Fla. 1:24-cr-20051 (ECF 61, 62) | The day 1 transcript is an excerpt, not the whole day. |
| United States v. Kotey, E.D. Va. 1:20-cr-00239 (ECF 354) | Only the last day was checked; its caption names El Shafee Elsheikh, and the case was not pursued. |
