// Severity is the AI's qualitative call, 3 to 0; the backing is the number it rests on.
window.SEV = {
  d1:[0,"59.5% vs 5.8%, settled by the sentence itself"], d2:[3,"28% of records · 'never terminated' tops out at 94%"],
  d3:[2,"≤ 18 cases · 0 of 18 settled"], d4:[2,"0 person keys on 9,806 rows"], d5:[2,"7.6% vs published 10%"],
  d6:[1,"5 date fields, none counted"], d7:[0,"9,505 vs 9,543"], d8:[0,"23 redactions, 0 in scope"], d9:[0,"204 of 509,912 cells"],
  l1:[2,"$1.58m · $63.0m → $61.4m"], l2:[1,"no count until a later export"], l3:[0,"$86.7m, on the in-house figure"], l4:[1,"not examined"],
  c1:[2,"1.63 – 2.93 across cuts"], c2:[3,"PPV .650 vs .595"], c3:[2,"no offending measure in the data"], c4:[1,"894 of 6,172"],
  c5:[0,"ratio 1.923, unchanged"], c6:[0,"1.87 – 1.92 across the sweep"], c7:[1,"1 county"], c8:[1,"137 questions, unreleased"]
};
window.SEV_WORD = ["checked, quiet", "worth a footnote", "moves the figure, or bounds it", "could not stand as written"];

// What the AI has already attacked: its own rewrites. Taking one swaps the sentence and the round.
window.ROUNDS = {
  d4: { sentence:["A vast majority"," of ","inmate-abuse cases"," ended ","without a dismissal","."], phrases:[0,2,4],
        value:"at most 94%", label:"Inmate-abuse cases that ended without a dismissal",
        carry:{d1:4, d2:0, d3:4, d5:2, d6:2, d7:2, d8:2, d9:2}, retired:["d4"], overrides:{}, added:[] },
  l1: { sentence:["US clients"," paid outside firms ","$61.4m"," ","to lobby on AI"," in the ","first half of 2026","."], phrases:[0,2,4,6],
        value:"$61,447,729", label:"Paid by US clients to outside firms to lobby on AI",
        carry:{l2:6, l3:2, l4:4}, retired:["l1"], overrides:{},
        added:[{id:"l1r", cls:"coverage", phrase:0, sev:2, backing:"$500,000 · $61.4m → $61.9m",
          text:"'US clients' now rests on client_country, and 8 filings record no country at all.",
          evidence:"'* Undetermined', 8 filings, $500,000. A != 'USA' test puts them out; a == 'USA' test agrees. Either way they are a decision, and the first decimal of $61.4m turns on it.",
          moves:{kind:"moves", detail:"$61.4m or $61.9m."}, cost:"free",
          qualifier:"8 filings, $500,000, record no client country and are counted as ___."}] },
  c2: { sentence:["Among defendants labelled high risk",", ","Black and white defendants"," ","reoffended"," at similar rates, ","65.0% and 59.5%","."], phrases:[0,2,4,6],
        value:"1.09", label:"Positive predictive value, Black over white",
        carry:{c1:0, c3:4, c4:2, c5:6, c6:2, c7:2, c8:0}, retired:["c2"],
        overrides:{
          c5:{sev:2, backing:".650/.595 → .566/.491", moves:{kind:"moves", detail:"Ratio 1.09 → 1.15. The error that left the first claim alone moves this one."},
              evidence:"All 868 affected rows are recidivists, and predictive value is measured on those labelled high risk, so fixing the window moves both rates down and widens the gap."},
          c1:{sev:1, backing:"not recomputed for predictive value", moves:{kind:"unpriced", detail:"The cut changes who is 'labelled high risk'. PPV at other cuts was not computed."}}
        },
        added:[{id:"c2r", cls:"semantic", phrase:6, sev:3, backing:"FPR .423 vs .220",
          text:"Measure the other error. Of those who did not reoffend, 42.3% of Black and 22.0% of white defendants were labelled high risk.",
          evidence:"Both are true at once. Chouldechova 2017: equal predictive values and equal false-positive rates cannot both hold when base rates differ, 52.3% vs 39.1%.",
          moves:{kind:"meaning", detail:"By false-positive rate the tool is twice as harsh on Black defendants. The sentence reverses without a number changing."},
          cost:"editorial", raised:"ProPublica 2016", back:true,
          qualifier:"Compares predictive values. By false-positive rate Black defendants are flagged wrongly 1.92 times as often."}] }
};

// What the run holds: the only things the AI reads to judge a sentence. Values are the run's or the methodology's.
window.EVID = {
  "doccs-terminated": [
    {id:"job_status_breakdown", kind:"table", name:"Inmate-abuse cases by job-loss outcome", value:"lost_job 5.8% · unknown 28% · kept_job the rest", note:"figure5_counts, read from PENALTYDIS"},
    {id:"total_investigations", kind:"figure", name:"Misconduct investigations, 2000–2020", value:"9,505 or 9,543", note:"by whether every rank spelling is matched"},
    {id:"pct_inmate_abuse", kind:"figure", name:"Share involving accusations of inmate abuse", value:"7.6%", note:"any code is IA · The Times published 10"},
    {id:"extreme_abuse_count", kind:"figure", name:"Occasions of extreme physical abuse", value:"0 of 289 confirmed", note:"review queue not yet worked"},
    {id:"extracted-data.csv", kind:"input", name:"extracted-data.csv", value:"9,806 rows × 53 columns", note:"one row per case · s_GUID and SSNUMBER empty on every row"},
    {id:"penalty_lookup", kind:"input", name:"penalty_lookup", value:"PENALTYDIS text → dismissal / ambiguous", note:"human-editable"},
    {id:"rank_lookup", kind:"input", name:"rank_lookup", value:"25 TITLE spellings → 4 ranks or blank", note:"human-editable"},
    {id:"methodology", kind:"document", name:"The methodology", value:"the Times note, and the register of choices", note:"where the 59.5% vs 5.8% reading is settled"}
  ],
  "lda-outside-spend": [
    {id:"ai_spend_totals", kind:"figure", name:"Paid to outside firms to lobby on AI", value:"$63,027,729", note:"sum of income over 1,295 paid filings"},
    {id:"in_house_ai_totals", kind:"figure", name:"Reported by organisations for their own AI lobbying", value:"$344,314,714.91", note:"547 filings"},
    {id:"corpus_totals", kind:"figure", name:"Rows read across both exports", value:"45,061 rows · 42,918 filings", note:""},
    {id:"lda_exports", kind:"input", name:"US Senate LDA exports, Q1 and Q2 2026", value:"2 files", note:"income, expenses, registrant, client, client_country, type"},
    {id:"arms", kind:"lineage", name:"The paid / in-house branch, per row", value:"else 1,297 · elif 547 · if 221", note:"recorded while the code ran · no arm reads client_country"},
    {id:"ai_rule", kind:"stage", name:"The rule that calls a filing AI lobbying", value:"not read", note:"the probe did not reach it"}
  ],
  "compas-fpr": [
    {id:"fpr", kind:"figure", name:"False-positive rate, Black / white", value:".423 / .220 · ratio 1.923", note:"n = 6,172"},
    {id:"ppv", kind:"figure", name:"Positive predictive value, Black / white", value:".650 / .595", note:"computed by the challenges mock, not a figure of the run"},
    {id:"base", kind:"figure", name:"Two-year re-arrest rate, Black / white", value:"52.3% / 39.1%", note:""},
    {id:"scores", kind:"input", name:"compas-scores-two-years.csv", value:"7,214 rows", note:"1,042 dropped by the ±30-day filter · 894 in neither compared group"},
    {id:"high_risk", kind:"stage", name:"high = score_text != Low", value:"decile ≥ 5, roughly", note:"the vendor's manual says 7"},
    {id:"recid", kind:"stage", name:"two_year_recid", value:"re-arrest within two years", note:"not conviction, not offending"}
  ]
};

// Where each load-bearing phrase lands in that evidence, per round. null = nothing in the run.
window.GROUND = {
  "doccs-terminated": {
    original: {0:["job_status_breakdown","the share is read off this table, and 28% of it is unknown"], 2:["extracted-data.csv","a row is a case; nothing keys a person"], 4:["pct_inmate_abuse","any misconduct code is IA · 725 records"], 6:["penalty_lookup","dismissal, read from the post-disposition penalty"]},
    d4: {0:["job_status_breakdown","the share is read off this table, and 28% of it is unknown"], 2:["extracted-data.csv","a row is a case, which is what the sentence now says"], 4:["penalty_lookup","not a dismissal, read from the post-disposition penalty"]}
  },
  "lda-outside-spend": {
    original: {1:["ai_spend_totals","the figure, to the tenth of a million"], 3:["ai_rule","a rule the probe did not read"], 5:["lda_exports","Q1 and Q2 filings, as exported"]},
    l1: {0:["arms","client_country, which no arm of the code reads"], 2:["ai_spend_totals","recomputed by the probe on US rows: $61,447,729. Not a figure of the run."], 4:["ai_rule","a rule the probe did not read"], 6:["lda_exports","Q1 and Q2 filings, as exported"]},
    apples: {0:null, 2:null, 4:null}
  },
  "compas-fpr": {
    original: {0:["scores","the two groups kept by the race filter"], 2:["fpr",".423 over .220"], 4:["high_risk","labelled high, and did not reoffend"], 6:["recid","re-arrest within two years"]},
    c2: {0:["high_risk","labelled high, at the published cut"], 2:["scores","the two groups kept by the race filter"], 4:["recid","re-arrest within two years"], 6:["ppv","computed by the challenges mock, not a figure of the run"]}
  }
};

// A sentence the run cannot back, attacked. Only on the lobbying run, as asked.
ROUNDS.apples = { only:"lda-outside-spend", sentence:["Apples"," are ","made of"," ","elephants","."], phrases:[0,2,4],
  value:"no figure", label:"Nothing in the run backs this sentence", carry:{}, retired:["l1","l2","l3","l4"], overrides:{},
  lede:"No phrase of this sentence lands on anything the run holds. There is no number to move, because none was cited; every word is unsupported rather than wrong.",
  added:[
    {id:"a1", cls:"gap", phrase:0, sev:3, backing:"0 of 45,061 rows", text:"Nothing in the run is about apples.", evidence:"The two inputs are Senate lobbying exports. No column, figure, term or stage names a fruit, a crop or a food.", moves:{kind:"meaning", detail:"Unsupported. The run cannot say this is false either."}, cost:"outside"},
    {id:"a2", cls:"gap", phrase:2, sev:3, backing:"0 figures", text:"The run computes what was paid and by whom. It says nothing about what anything is made of.", evidence:"Every figure is a sum of reported income or expenses, or a count of filings. There is no composition, production or manufacture in it.", moves:{kind:"meaning", detail:"Unsupported."}, cost:"outside"},
    {id:"a3", cls:"gap", phrase:4, sev:3, backing:"0 of 45,061 rows", text:"Nothing in the run is about elephants.", evidence:"Neither export carries an animal, and no filing's issue text was read for one.", moves:{kind:"meaning", detail:"Unsupported."}, cost:"outside"}
  ] };

// One orchestrator, six attackers. Each reads a different part of the run and raises one kind of challenge.
window.ATTACKERS = [
  {id:"grounding", name:"Grounding", cls:["gap"], reads:"the sentence against the pool", does:"lands every load-bearing phrase on a figure, column, term or stage, or on nothing"},
  {id:"defects", name:"Data defects", cls:["data"], reads:"the input files and how each stage reads them", does:"malformed cells, two spellings of one thing, a reading that disagrees with its source"},
  {id:"choices", name:"Choices made", cls:["choice"], reads:"stage code and the recorded arms", does:"every threshold, field and cut, swept to its alternatives"},
  {id:"omissions", name:"Decisions never made", cls:["omission"], reads:"input columns against the recorded arms", does:"a column that partitions the rows and that no branch reads"},
  {id:"coverage", name:"Coverage", cls:["coverage"], reads:"filters' dropped rows and the shape's open/closed word", does:"who is in the count, who is not, and what the file does not hold"},
  {id:"meaning", name:"Meaning", cls:["semantic","causal"], reads:"the sentence, the stage descriptions and the terms", does:"the same number read as a different sentence, and the rewrites the run also supports"}
];
window.attackerFor = cls => ATTACKERS.find(a => a.cls.includes(cls)) || ATTACKERS[0];

// Where each claim starts in the mock. Approving on its page moves it.
window.DEFAULT_STATUS = {"doccs-terminated":"submitted", "lda-outside-spend":"submitted", "compas-fpr":"approved"};

// A table claim, parked: shown only with ?tables=1 in the address. The map is the table itself.
if (location.search.includes("tables=1")) CLAIMS.push({
  id: "compas-table", kind: "table", project: "compas_machine_bias", run: "20260902T134800", stage: "errors_by_race",
  value: "2 × 2", label: "How COMPAS errs, by race", coverage: "closed", context: "Broward County · 2013–14 intake · n = 6,172",
  sentence: ["COMPAS", " ", "errs differently", " for ", "Black and white defendants", "."], phrases: [0, 2, 4],
  table: {
    columns: [{id:"fpr", label:"false-positive rate", note:"labelled high, did not reoffend"}, {id:"base", label:"two-year re-arrest rate", note:""}],
    rows: [{id:"black", label:"Black defendants", cells:{fpr:".423", base:"52.3%"}}, {id:"white", label:"white defendants", cells:{fpr:".220", base:"39.1%"}}],
    frame: [{id:"rows", label:"2 rows"}, {id:"group", label:"grouped by race, as recorded"}, {id:"n", label:"n = 6,172 · no n per row"}]
  },
  qualifiers: ["High risk means a COMPAS score above Low, roughly decile 5 and up."],
  lede: "The table shows one of the tool's two errors. The other, predictive value, is near-even across the same rows, and with base rates this far apart the two cannot both be equal. Whether 'errs differently' is true depends on which column is on the page.",
  stages: [], shared_stages: {},
  challenges: [
    {id:"t8", cls:"semantic", phrase:2, sev:3, backing:"PPV .650 vs .595, not in the table",
     text:"The other error is not a column. Of those labelled high risk, 65.0% of Black and 59.5% of white defendants reoffended.",
     evidence:"A reader of this table sees one error and no denominator for the other. Chouldechova 2017: with base rates of 52.3% and 39.1%, equal predictive value forces unequal false-positive rates.",
     moves:{kind:"meaning", detail:"With the column added, the caption's 'differently' is true of one column and false of the other."}, cost:"editorial", raised:"Flores, Bechtel & Lowenkamp 2016; Northpointe",
     alt:{label:"How COMPAS errs, by race, both errors", value:"2 × 3", note:"The same rows with the rebuttal's column beside the story's. The reader decides which error matters."},
     qualifier:"Shows the false-positive rate only. By predictive value the two groups are near-even (65.0% vs 59.5%)."},
    {id:"t1", cls:"choice", phrase:"col:fpr", sev:2, backing:"1.63 – 2.93 across cuts",
     text:"The column depends on the cut. The vendor's own manual treats a score as high from decile 7.",
     evidence:"Ratio across cuts: ≥ 4: 1.63 · published: 1.92 · ≥ 7: 2.75 · ≥ 8: 2.93. Both cells move; the gap widens at every cut but one.",
     moves:{kind:"moves", detail:"Both cells of this column."}, cost:"free", raised:"Northpointe disputed the binarisation",
     qualifier:"At other risk cuts the ratio of the two false-positive rates runs 1.63 to 2.93."},
    {id:"t7", cls:"semantic", phrase:"col:base", sev:2, backing:"re-arrest, not offending",
     text:"'Re-arrest' is the ground truth for every column here. If policing is unequal, the label carries the bias being measured.",
     evidence:"two_year_recid records arrest within two years, not conviction and not offending. Nothing in the data settles it.",
     moves:{kind:"unpriced", detail:"Needs conviction records or victimisation surveys."}, cost:"outside",
     qualifier:"'Reoffended' means re-arrested within two years."},
    {id:"t3", cls:"coverage", phrase:"frame:rows", sev:1, backing:"894 of 6,172",
     text:"Two rows. 894 defendants are in the data and in neither.",
     evidence:"Hispanic 509 · Other 343 · Asian 31 · Native American 11.",
     moves:{kind:"unpriced", detail:"A scope fact."}, cost:"free",
     qualifier:"Black and white defendants only; 894 others are in the data and not in this table."},
    {id:"t4", cls:"coverage", phrase:"frame:n", sev:1, backing:"no n per row",
     text:"A rate per row with no count per row. The run holds the group sizes; the table does not show them.",
     evidence:"A reader cannot tell a rate over 3,000 defendants from one over 300.",
     moves:{kind:"unpriced", detail:"A column, not a rerun."}, cost:"free",
     qualifier:"Group sizes are on the run page."},
    {id:"t6", cls:"coverage", phrase:"frame:group", sev:1, backing:"an administrative field",
     text:"Race is as recorded by Broward County at intake.",
     evidence:"The grouping column is a jail record, not self-identification.",
     moves:{kind:"unpriced", detail:"A scope fact."}, cost:"outside"},
    {id:"t10", cls:"data", phrase:"col:fpr", sev:0, backing:"ratio 1.923, unchanged",
     text:"The two-year window was applied to non-recidivists but not to recidivists.",
     evidence:"Fixing it drops n to 5,304. All 868 affected rows are recidivists, and this column is measured on non-recidivists, so neither cell moves.",
     moves:{kind:"none", detail:"Would move a predictive-value column, if there were one."}, cost:"free", raised:"Barenstein 2019"},
    {id:"t9", cls:"choice", phrase:"frame:group", sev:0, backing:"1.87 – 1.92 across the sweep",
     text:"The ±30-day screening filter, swept from ±7 days to no limit.",
     evidence:"n 5,757–6,907; the ratio of the two cells stays 1.87–1.92.",
     moves:{kind:"none", detail:"Stable."}, cost:"free"}
  ]
});
Object.assign(SEV, { t8:[3,"PPV .650 vs .595, not in the table"], t1:[2,"1.63 – 2.93 across cuts"], t7:[2,"re-arrest, not offending"], t3:[1,"894 of 6,172"], t4:[1,"no n per row"], t6:[1,"an administrative field"], t10:[0,"ratio 1.923, unchanged"], t9:[0,"1.87 – 1.92 across the sweep"], t2:[2,".650/.595 → .566/.491"], t5:[3,"52.3% vs 39.1%"] });
EVID["compas-table"] = EVID["compas-fpr"];
GROUND["compas-table"] = {
  original: {0:["scores","the decile scores in the file"], 2:["fpr","one of the tool's two errors"], 4:["scores","the two groups the race filter keeps"],
    "col:fpr":["fpr","labelled high, did not reoffend, per group"], "col:base":["base","re-arrested within two years, per group"],
    "row:black":["scores","race == African-American"], "row:white":["scores","race == Caucasian"],
    "frame:rows":["scores","the race filter keeps two of six values"], "frame:group":["scores","the race column, as recorded"], "frame:n":["fpr","n = 6,172 after the ±30-day filter"]},
  t8: {0:["scores","the decile scores in the file"], 2:["fpr","one error, beside the other"], 4:["scores","the two groups the race filter keeps"],
    "col:fpr":["fpr","labelled high, did not reoffend, per group"], "col:ppv":["ppv","computed by the challenges mock, not a figure of the run"], "col:base":["base","re-arrested within two years, per group"],
    "row:black":["scores","race == African-American"], "row:white":["scores","race == Caucasian"],
    "frame:rows":["scores","the race filter keeps two of six values"], "frame:group":["scores","the race column, as recorded"], "frame:n":["fpr","n = 6,172 after the ±30-day filter"]}
};
// The rewrite of a table is a column added, a column dropped, or a regrouping. Here: a column added, attacked already.
ROUNDS.t8 = { only:"compas-table", sentence:["COMPAS", " ", "errs differently", " for ", "Black and white defendants", "."], phrases:[0,2,4],
  value:"2 × 3", label:"How COMPAS errs, by race, both errors",
  table: {
    columns: [{id:"fpr", label:"false-positive rate", note:"labelled high, did not reoffend"}, {id:"ppv", label:"positive predictive value", note:"labelled high, did reoffend"}, {id:"base", label:"two-year re-arrest rate", note:""}],
    rows: [{id:"black", label:"Black defendants", cells:{fpr:".423", ppv:".650", base:"52.3%"}}, {id:"white", label:"white defendants", cells:{fpr:".220", ppv:".595", base:"39.1%"}}],
    frame: [{id:"rows", label:"2 rows"}, {id:"group", label:"grouped by race, as recorded"}, {id:"n", label:"n = 6,172 · no n per row"}]
  },
  carry:{t1:"col:fpr", t7:"col:base", t3:"frame:rows", t4:"frame:n", t6:"frame:group", t9:"frame:group", t10:"col:ppv"}, retired:["t8"],
  overrides:{ t10:{sev:2, backing:".650/.595 → .566/.491", moves:{kind:"moves", detail:"Both cells of the new column. Ratio 1.09 → 1.15."}, evidence:"All 868 affected rows are recidivists, and predictive value is measured on those labelled high risk, so fixing the window moves both cells down and widens the gap. The false-positive column is untouched."} },
  lede:"With both errors on the page the caption holds for one column and fails for the other. The data error that left the first column alone now moves the second.",
  added:[{id:"t5", cls:"semantic", phrase:2, sev:3, backing:"52.3% vs 39.1%",
    text:"Two columns that cannot both be equal. Reading the first as bias and the second as fairness is one fact stated twice.",
    evidence:"Chouldechova 2017: when base rates differ, equal predictive value forces unequal false-positive rates. The table now shows the theorem rather than a finding.",
    moves:{kind:"meaning", detail:"The caption needs to say which error it means."}, cost:"editorial", raised:"Chouldechova 2017",
    qualifier:"The two error columns cannot both be equal when re-arrest rates differ, and they do."}] };
DEFAULT_STATUS["compas-table"] = "submitted";
