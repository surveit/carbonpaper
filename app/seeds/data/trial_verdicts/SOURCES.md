# Sources: nine federal criminal jury trials, verdicts included

A mirror of 31 transcript files and 4 judgments from CourtListener's RECAP archive, read by the `recap_files` connector through the `trial_verdicts` bundle's `cache_dir`, so a run never calls CourtListener. `manifest.jsonl` is the fetch record for every file here, with paths relative to this directory.

## Terms

RECAP archive file on storage.courtlistener.com, Free Law Project. ToS https://wiki.free.law/c/terms/courtlistener/courtlistenercom-terms-of-service-and-policies (modified 2026-08-05): filings are 'generally in the public domain' but may contain third-party copyrighted works; do not present the data so as to suggest Free Law Project produced, endorsed, or verified an AI-generated analysis.

## How the first five were found

On 2026-11-11, CourtListener's RECAP search (www.courtlistener.com, in a browser; the search API now refuses anonymous calls) was run for transcript filings, available as PDFs, whose text holds "voir dire", "opening statement", "direct examination", "closing argument", "we, the jury" and "not guilty", and separately for "we the jury", "opening statement" and "closing argument". Each candidate's docket was then read for its other trial-day transcripts. A trial was kept when every day or volume of its trial, through the verdict, is in RECAP, the PDFs carry a text layer pypdf reads, the trial has one defendant, and the charge is not one that would put sexual-abuse material describing children into the repository.

Each of these five ended in conviction on every count. A transcript reaches RECAP when someone buys it, mostly for an appeal, which a defendant brings only after a conviction.

## How the last four were found

The first five cannot tell a model that weighs the evidence from one that always answers guilty, so a second search on the same day looked for trials whose jury acquitted on at least one count, kept to the same rules. CourtListener's RECAP search was run in a browser for transcript filings (`description:transcript`, PDFs only) whose text holds the words of a not-guilty verdict being read:

- `"jury find the defendant not guilty"~4` (up to four words between, room for a defendant's name) with `polled` or `"poll the jury"`: 43 dockets. Brown and Branch came from here.
- `"verdict of not guilty"`, `"unanimously find the defendant not guilty"~5` or `"jury find not guilty"~6`, with `"is this your verdict"` (or "was this" or "is that your verdict"): 29 dockets. Reagor came from here.
- `"found you not guilty"`: 3 dockets. Bryant came from here, through its sentencing transcript (ECF 139), where the judge says "The same jury found you not guilty on Counts Three, Four, and Five".
- Other variants (`"not guilty as to count"`, `"not guilty as charged in count"`, `"hearken to your verdict"`, `"you are free to go"`, and docket entries described as a not-guilty jury verdict) turned up the rest of the candidates under Left out; Boen, Smith and Chambers came from the first query.

Each candidate's docket was then read for its verdict entry and its trial-day transcripts, and each kept trial's verdict was read where the transcript records it being read in court:

| Trial | The verdict as read in court | Where | What the transcript lacks |
|---|---|---|---|
| United States v. Brown, N.D. Ill. 1:14-cr-00674 | Count 1 (unreasonable force under color of law): guilty, with bodily injury. Counts 2 and 3 (falsifying documents in a federal investigation): not guilty. | ECF 123, PDF page 624 | ECF 123 skips the closing arguments, at PDF page 580: "(Closing arguments reported but not transcribed at this time.)" They were filed later as ECF 137, an excerpt running from the government's closing through its rebuttal, after the court granted the government's motion to supplement the record on appeal (ECF 129, 131). ECF 137 is listed first, so it sits before the cut where deliberations began; in the courtroom it came between ECF 123's pages 580 and 581. |
| United States v. Branch, N.D. Ala. 2:15-cr-00154 | Count 1: guilty. Counts 2 to 16: not guilty. | ECF 46, PDF page 16 (transcript page 834) | Nothing found missing. The docket says Volume III was held 8/13/15; its cover says August 12, 2015, which the description follows. |
| United States v. Bryant, M.D. Tenn. 3:18-cr-00144 | Counts 1 and 2 (deprivation of rights under color of law): guilty. Counts 3 and 4 (falsifying a document) and 5 (false statement): not guilty. | ECF 116, PDF pages 95 to 96 | Jury selection, "filed as Trial Volume IA" (ECF 113, page 5), is ECF 117, sealed. This is the trial of January 7 to 10, 2020; transcripts of an earlier trial of the case (February 4 to 8, 2019, ECF 62 to 67) are on the docket and are not used. |
| United States v. Reagor, N.D. Tex. 2:21-cr-00025 | Counts 1 and 2 (bank fraud): not guilty. Count 3 (false statement to a bank): guilty. | ECF 168, PDF page 123 (transcript page 730) | Jury selection and two bench conferences are "filed under separate sealed volume" (ECF 163 index). ECF 161, a 12-page hearing of October 11, 2021 giving the parties the court's limits on opening statements, is not one of the four trial volumes and is left out. |

The docket's own verdict entries say the same: Brown ECF 97, Branch ECF 47, Bryant ECF 104, Reagor ECF 97. Each of the four defendants was convicted on at least one count.

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
| United States v. Brown | 137 | `pdfs/ilnd-1-14-cr-00674_ecf0137.pdf` | 60 | Closing Arguments during Jury Trial, held October 22, 2015 (excerpt) | https://storage.courtlistener.com/recap/gov.uscourts.ilnd.303368.137.0.pdf | `eed13805cd6c7bd99db19a57b3a044f37264ad6f1a3b3f709ea46b24d9b40ba4` | 2026-11-11T19:13:32Z |
| United States v. Brown | 123 | `pdfs/ilnd-1-14-cr-00674_ecf0123.pdf` | 627 | Jury Trial, Vol 1 - Vol 5B, held October 19 to 23, 2015 | https://storage.courtlistener.com/recap/gov.uscourts.ilnd.303368.123.0.pdf | `d790468ad8b3f853e2e36d9ca375bc4f833a70bebd4513df1be95bbfc8744d4e` | 2026-11-11T19:13:33Z |
| United States v. Bryant | 113 | `pdfs/tnmd-3-18-cr-00144_ecf0113.pdf` | 103 | Trial Volume 1B, held January 7, 2020 | https://storage.courtlistener.com/recap/gov.uscourts.tnmd.74996/gov.uscourts.tnmd.74996.113.0.pdf | `f74241429b54aef8632e14b5cc3e45e3c1334626efc15ec10910673f32809dab` | 2026-11-11T19:13:34Z |
| United States v. Bryant | 114 | `pdfs/tnmd-3-18-cr-00144_ecf0114.pdf` | 292 | Trial Volume 2, held January 8, 2020 | https://storage.courtlistener.com/recap/gov.uscourts.tnmd.74996/gov.uscourts.tnmd.74996.114.0.pdf | `feab38653d3f7ea20833e1968e4179eb0d9e6b95dcf99daebb7ec4abc07794fd` | 2026-11-11T19:13:36Z |
| United States v. Bryant | 115 | `pdfs/tnmd-3-18-cr-00144_ecf0115.pdf` | 245 | Trial Volume 3, held January 9, 2020 | https://storage.courtlistener.com/recap/gov.uscourts.tnmd.74996/gov.uscourts.tnmd.74996.115.0.pdf | `5ab4e9c494686cfde1641a30d4799f30356ddfb6290870221d94794d961efa9a` | 2026-11-11T19:13:37Z |
| United States v. Bryant | 116 | `pdfs/tnmd-3-18-cr-00144_ecf0116.pdf` | 99 | Trial Volume 4, held January 10, 2020 | https://storage.courtlistener.com/recap/gov.uscourts.tnmd.74996/gov.uscourts.tnmd.74996.116.0.pdf | `c95de22af49f1b4dcc0b84993a1799eef67a0b5a5b3bf68028b95a95fca6b327` | 2026-11-11T19:13:38Z |
| United States v. Branch | 42 | `pdfs/alnd-2-15-cr-00154_ecf0042.pdf` | 142 | Jury Trial, Vol. I of V, held August 10, 2015 | https://storage.courtlistener.com/recap/gov.uscourts.alnd.155347/gov.uscourts.alnd.155347.42.0.pdf | `8f68c38d1129247b4b6d7f31eb028f9c40c320a6c40f99783e93692744ba83a2` | 2026-11-11T19:13:39Z |
| United States v. Branch | 43 | `pdfs/alnd-2-15-cr-00154_ecf0043.pdf` | 292 | Jury Trial, Vol. II of V, held August 11, 2015 | https://storage.courtlistener.com/recap/gov.uscourts.alnd.155347/gov.uscourts.alnd.155347.43.0.pdf | `08165fab9952f3ab0a9dea927b0d8d07a3aaf8f04c20ee39766d5d8ef055e108` | 2026-11-11T19:13:41Z |
| United States v. Branch | 44 | `pdfs/alnd-2-15-cr-00154_ecf0044.pdf` | 333 | Jury Trial, Vol. III of V, held August 12, 2015 | https://storage.courtlistener.com/recap/gov.uscourts.alnd.155347/gov.uscourts.alnd.155347.44.0.pdf | `ff7eb455622c46447386486a83c58a5e5c6532e25239f10f410b674389b02332` | 2026-11-11T19:13:42Z |
| United States v. Branch | 45 | `pdfs/alnd-2-15-cr-00154_ecf0045.pdf` | 144 | Jury Trial, Vol. IV of V, held August 13, 2015 | https://storage.courtlistener.com/recap/gov.uscourts.alnd.155347/gov.uscourts.alnd.155347.45.0.pdf | `5364d40a824f5d222ca4cfacbc62af5e514db362ed99d8755b7526b6aed450ed` | 2026-11-11T19:13:44Z |
| United States v. Branch | 46 | `pdfs/alnd-2-15-cr-00154_ecf0046.pdf` | 20 | Jury Trial, Vol. V of V, held August 14, 2015 | https://storage.courtlistener.com/recap/gov.uscourts.alnd.155347/gov.uscourts.alnd.155347.46.0.pdf | `d20a99f380ebd4cc7bf04d389d4e2962b8ce966f6e58b3dc4b41e42f6d4bd00c` | 2026-11-11T19:13:45Z |
| United States v. Reagor | 163 | `pdfs/txnd-2-21-cr-00025_ecf0163.pdf` | 346 | Criminal Trial by Jury, Volume I of IV, held October 12, 2021 | https://storage.courtlistener.com/recap/gov.uscourts.txnd.347162/gov.uscourts.txnd.347162.163.0.pdf | `cf4577da034278367aa52526faa11cd644b0fc52d45f41ba451b4642ea364674` | 2026-11-11T19:13:47Z |
| United States v. Reagor | 165 | `pdfs/txnd-2-21-cr-00025_ecf0165.pdf` | 197 | Criminal Trial by Jury, Volume II of IV, held October 13, 2021 | https://storage.courtlistener.com/recap/gov.uscourts.txnd.347162/gov.uscourts.txnd.347162.165.0.pdf | `a96b019bcfb2f849745ef113f80e07a74509767d175e6e68145bc1389258650c` | 2026-11-11T19:13:48Z |
| United States v. Reagor | 167 | `pdfs/txnd-2-21-cr-00025_ecf0167.pdf` | 64 | Criminal Trial by Jury, Volume III of IV (Charge Conference), held October 13, 2021 | https://storage.courtlistener.com/recap/gov.uscourts.txnd.347162/gov.uscourts.txnd.347162.167.0.pdf | `5d7985f1f02416faf598d6d44a5ecd0e53e33d6ac3511cc659f05e95e64a68e7` | 2026-11-11T19:13:49Z |
| United States v. Reagor | 168 | `pdfs/txnd-2-21-cr-00025_ecf0168.pdf` | 130 | Criminal Trial by Jury, Volume IV of IV, held October 14 and 15, 2021 | https://storage.courtlistener.com/recap/gov.uscourts.txnd.347162/gov.uscourts.txnd.347162.168.0.pdf | `84a3b319b206463521947a12aa11ff4192cbe3905cb3779a8bbdd1c40528d46f` | 2026-11-11T19:13:51Z |

31 files, 5752 pages.

## Judgments

The `input_records` stage reads each trial's Judgment in a Criminal Case, a separate filing, as a second source for the jury's verdict.

| Trial | ECF | File | What it is | CourtListener URL |
|---|---|---|---|---|
| United States v. Joos | 90 | `pdfs/mowd-3-09-cr-05022_ecf0090.pdf` | Judgment and Commitment, May 20, 2010 | https://storage.courtlistener.com/recap/gov.uscourts.mowd.91111/gov.uscourts.mowd.91111.90.0.pdf |
| United States v. Atta | 256 | `pdfs/casd-3-21-cr-01289_ecf0256.pdf` | Judgment, January 12, 2026 | https://storage.courtlistener.com/recap/gov.uscourts.casd.706125/gov.uscourts.casd.706125.256.0.pdf |
| United States v. Craft | 76 | `pdfs/kywd-3-22-cr-00094_ecf0076.pdf` | Judgment and Commitment Order, July 28, 2023 | https://storage.courtlistener.com/recap/gov.uscourts.kywd.127039/gov.uscourts.kywd.127039.76.0.pdf |
| United States v. Tulali | 137 | `pdfs/akd-4-23-cr-00003_ecf0137.pdf` | Judgment, October 16, 2024 | https://storage.courtlistener.com/recap/gov.uscourts.akd.70443/gov.uscourts.akd.70443.137.0.pdf |

Their sha256s and fetch times are in `manifest.jsonl`.

- Joos: the Amended Judgment (ECF 100, correcting the title and section of Count 2) is not in RECAP; ECF 90 names the same counts of conviction.
- Atta: the judgment came more than three years after the 2022 trial. The docket shows the defendant's motions for a new trial (ECF 163, 170) denied on April 30, 2024 (ECF 209), so the judgment records this jury's verdict.
- Poke: neither the Judgment (ECF 179) nor the Amended Judgment (ECF 219) is in RECAP, so Poke has no second source.
- Brown, Bryant, Reagor: their judgments (ECF 115, 132 and 156) are on the docket but not in RECAP. Branch: CourtListener's copy of the docket ends at ECF 63, October 30, 2017, and holds no judgment. Their docket verdict entries, listed above, are the only second source, and the pipeline does not read them.

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
| United States v. Boen, W.D. Ark. 2:19-cr-20037 (ECF 93 to 98) | The docket's verdict entry (ECF 83) reads guilty on Counts 2 and 3, not guilty on Count 1, and all six trial days are in RECAP, but the PDFs are scans: pypdf reads only the ECF header line, about 90 characters a page. |
| United States v. Smith, D.N.M. 1:18-cr-03495 (ECF 242 to 246) | All five volumes are in RECAP, but Volume 5 (ECF 246), which holds the verdict, is a scan with no text layer pypdf reads; the other volumes were not checked. |
| United States v. Chambers, S.D.N.Y. 1:17-cr-00396 | The judgment (ECF 103) records guilty on counts 1 to 4, and only two of six trial days (ECF 81, 89) are in RECAP. |
| United States v. Colon, S.D.N.Y. 1:24-cr-00367 | The judgment (ECF 68) records guilty on counts 1 and 2, and the only trial transcript in RECAP is November 22, 2024 (ECF 56). |
| United States v. Ruta, filed as United States v. Winiewski, M.D. Fla. 8:24-cr-00429 | Two defendants. The verdict entry for Ruta (ECF 161) reads guilty on Count 2 and not guilty on Count 1; the trial transcripts were filed on 2026-11-09 (ECF 247, 248), and their docket text says they may be made public after ninety days. |
| United States v. Stinson, E.D. Va. 1:25-cr-00209 | The verdict entry (ECF 123) reads not guilty on Count 1s; the transcripts found (ECF 125, 126) are excerpts, one witness's testimony and the government's closing. |
| United States v. Danchenko, E.D. Va. 1:21-cr-00245 | The day 6 transcript (ECF 129) reads an acquittal. Not pursued: the Special Counsel's prosecution was widely reported, so a model likely knows the outcome (an inference, not tested). |
| United States v. Salman, M.D. Fla. 6:17-cr-00018 | Not pursued: a widely reported trial, and the volume the search found (ECF 379) is described as excluding closing arguments. |
| McGriff v. Reardon, E.D.N.Y. 1:21-cv-00703 (ECF 6-1 to 6-5) | A habeas response attaching a New York state trial transcript whose verdict reading includes "not guilty as to count one, attempted assault in the first degree". Not pursued: the bundle's prompts describe a federal trial. |
| Augustine v. Chicopee, D. Mass. 3:20-cv-30177 (ECF 58-2) | A civil rights suit attaching 27 pages of a two-day Massachusetts district court trial, an excerpt; the verdict it records is guilty on both complaints. |

Leads that the searches turned up with a verdict-day transcript in RECAP, whose verdicts and other trial days were not checked: United States v. Xiao, S.D. Ill. 4:21-cr-40039; United States v. Nettleton, M.D. Fla. 3:19-cr-00001; United States v. Baxter, D. Mass. 1:23-cr-10001; United States v. Sulik, E.D. Ky. 5:18-cr-00020; United States v. Cooper, D. Ariz. 4:10-cr-00239; United States v. Walker, S.D. Tex. 4:23-cr-00042; United States v. Delgado, D. Ariz. 4:19-cr-01094; United States v. Vasquez-Perez, S.D. Cal. 3:20-cr-03445.
