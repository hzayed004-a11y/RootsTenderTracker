"""Lexicons driving the pharma filter and the oncology / therapeutic-area classifier.

Design notes
------------
* Oncology is detected from molecules, indications, modalities and drug-class
  morphology -- not from the word "oncology". A tender reading
  "Supply of Trastuzumab Deruxtecan 100mg vials" contains no oncology keyword.
* Suffix rules catch molecules that post-date this file. Every monoclonal
  antibody ends -mab, kinase inhibitors -tinib, PARP inhibitors -parib,
  CDK4/6 -ciclib, proteasome -zomib, ADC -tecan/-vedotin/-deruxtecan.
  These are WHO INN stems, so they are stable and forward-compatible.
* Arabic terms are included because Kuwait MOH listings are frequently Arabic
  or mixed. Matching is done on normalised text (see normalize.arabic_fold).
"""
from __future__ import annotations

# --------------------------------------------------------------------------
# 1. Is this a pharmaceutical tender at all?
# --------------------------------------------------------------------------

PHARMA_TERMS = {
    # English
    "medicine", "medicines", "medicinal", "drug", "drugs", "pharmaceutical",
    "pharmaceuticals", "pharmacy", "medication", "medications", "formulation",
    "active ingredient", "api", "generic", "innovator", "biosimilar",
    "biologic", "vaccine", "vaccines", "serum", "antibiotic", "antibiotics",
    "injection", "injectable", "vial", "vials", "ampoule", "ampoules",
    "tablet", "tablets", "capsule", "capsules", "syrup", "suspension",
    "solution for infusion", "infusion", "sachet", "suppository", "ointment",
    "cream", "eye drops", "inhaler", "nebuliser", "nebulizer", "prefilled syringe",
    "iv fluid", "parenteral", "oral solution", "lyophilized", "powder for solution",
    "insulin", "pen", "prefilled pen", "biologic medicine",
    "gmp", "who-gmp", "pharmacopoeia", "bp ", " usp", "shelf life", "batch number",
    "marketing authorization", "drug registration", "kdfc", "mhra", "ema", "fda",
    # Arabic
    "دواء", "ادوية", "الادوية", "عقار", "عقاقير", "مستحضرات صيدلانية",
    "مستحضر", "صيدلانية", "صيدلية", "حقن", "حقنة", "امبولات", "اقراص",
    "كبسولات", "شراب", "محلول", "مرهم", "لقاح", "لقاحات", "مضاد حيوي",
    "محاليل وريدية", "تسجيل دوائي",
}

# Hard exclusions: if the listing is dominated by these and has no pharma
# signal, it is not our business.
NON_PHARMA_TERMS = {
    "construction", "civil works", "building", "renovation", "maintenance of building",
    "hvac", "air conditioning", "elevator", "lift maintenance", "painting",
    "furniture", "office supplies", "stationery", "printing", "catering",
    "cleaning", "janitorial", "security services", "manpower supply",
    "vehicle", "vehicles", "bus", "fuel", "generator", "landscaping",
    "software", "hardware", "server", "network", "laptop", "printer",
    "licenses", "it services", "cctv", "cabling", "data centre", "data center",
    "uniform", "laundry", "insurance", "consultancy services",
    "إنشاء", "مباني", "صيانة مباني", "أثاث", "قرطاسية", "تنظيف",
    "حراسة", "مركبات", "وقود", "حاسب آلي", "برمجيات", "شبكات",
}

# Medical but not pharmaceutical -- biomedical engineering department will be
# full of these. Flagged separately so they can be reported, not silently lost.
DEVICE_TERMS = {
    "equipment", "device", "devices", "machine", "analyzer", "analyser",
    "mri", "ct scanner", "x-ray", "ultrasound", "ventilator", "monitor",
    "surgical instrument", "spare parts", "calibration", "endoscope",
    "dental chair", "autoclave", "centrifuge", "incubator",
    "أجهزة", "جهاز", "معدات طبية", "قطع غيار",
    # consumables that ride along with device tenders
    "disposable", "consumable", "reagent", "reagents", "test kit", "cartridge",
    "catheter", "syringe needle", "glove", "gauze", "suture",
}

# --------------------------------------------------------------------------
# 2. Oncology
# --------------------------------------------------------------------------

ONCOLOGY_CONTEXT = {
    "oncology", "oncological", "cancer", "carcinoma", "sarcoma", "melanoma",
    "lymphoma", "leukaemia", "leukemia", "myeloma", "neoplasm", "neoplastic",
    "antineoplastic", "anti-neoplastic", "cytotoxic", "cytostatic",
    "chemotherapy", "chemotherapeutic", "immunotherapy", "immuno-oncology",
    "targeted therapy", "radiotherapy", "brachytherapy", "tumour", "tumor",
    "metastatic", "metastasis", "adjuvant", "neoadjuvant", "palliative",
    "haemato-oncology", "hemato-oncology", "haematology", "hematology",
    "checkpoint inhibitor", "car-t", "antibody drug conjugate", "adc",
    "tyrosine kinase inhibitor", "monoclonal antibody", "cytotoxic reconstitution",
    "bone marrow transplant", "stem cell transplant", "myelosuppression",
    "febrile neutropenia", "oncology day care",
    # Arabic
    "أورام", "الأورام", "ورم", "سرطان", "السرطان", "سرطانية", "كيماوي",
    "العلاج الكيماوي", "علاج مناعي", "مناعي", "لوكيميا", "ليمفوما",
    "نخاع العظم", "خلايا جذعية", "أدوية الأورام",
}

CANCER_INDICATIONS = {
    "breast cancer", "lung cancer", "nsclc", "sclc", "colorectal cancer",
    "colon cancer", "rectal cancer", "gastric cancer", "oesophageal cancer",
    "esophageal cancer", "pancreatic cancer", "hepatocellular carcinoma", "hcc",
    "prostate cancer", "bladder cancer", "renal cell carcinoma", "rcc",
    "ovarian cancer", "cervical cancer", "endometrial cancer",
    "head and neck cancer", "thyroid carcinoma", "glioblastoma", "glioma",
    "neuroblastoma", "osteosarcoma", "ewing sarcoma", "gist",
    "multiple myeloma", "hodgkin", "non-hodgkin", "dlbcl", "follicular lymphoma",
    "mantle cell", "cll", "aml", "all", "cml", "mds", "myelofibrosis",
    "polycythaemia vera", "polycythemia vera", "essential thrombocythaemia",
    "mesothelioma", "cholangiocarcinoma", "nasopharyngeal carcinoma",
}

# Named molecules. Not exhaustive by design -- the suffix rules below carry
# the long tail. These are the ones whose names give no morphological clue.
ONCOLOGY_MOLECULES = {
    # classical cytotoxics
    "cisplatin", "carboplatin", "oxaliplatin", "nedaplatin",
    "cyclophosphamide", "ifosfamide", "bendamustine", "melphalan",
    "chlorambucil", "busulfan", "lomustine", "carmustine", "temozolomide",
    "dacarbazine", "procarbazine", "thiotepa", "trabectedin",
    "doxorubicin", "liposomal doxorubicin", "epirubicin", "daunorubicin",
    "idarubicin", "mitoxantrone", "bleomycin", "dactinomycin", "mitomycin",
    "vincristine", "vinblastine", "vinorelbine", "vindesine", "vinflunine",
    "paclitaxel", "nab-paclitaxel", "docetaxel", "cabazitaxel",
    "etoposide", "teniposide", "irinotecan", "topotecan",
    "methotrexate", "pemetrexed", "pralatrexate", "raltitrexed",
    "fluorouracil", "5-fu", "capecitabine", "tegafur", "gemcitabine",
    "cytarabine", "azacitidine", "decitabine", "fludarabine", "cladribine",
    "clofarabine", "nelarabine", "mercaptopurine", "thioguanine",
    "hydroxyurea", "hydroxycarbamide", "asparaginase", "pegaspargase",
    "arsenic trioxide", "tretinoin", "atra", "bortezomib", "carfilzomib",
    "lenalidomide", "pomalidomide", "thalidomide",
    # hormonal
    "tamoxifen", "anastrozole", "letrozole", "exemestane", "fulvestrant",
    "bicalutamide", "flutamide", "nilutamide", "abiraterone", "enzalutamide",
    "apalutamide", "darolutamide", "goserelin", "leuprorelin", "leuprolide",
    "triptorelin", "degarelix", "octreotide", "lanreotide", "megestrol",
    # targeted / immuno that break the suffix rules
    "everolimus", "temsirolimus", "sirolimus", "venetoclax", "olaparib",
    "lenvatinib", "regorafenib", "sorafenib", "sunitinib", "pazopanib",
    "trastuzumab emtansine", "trastuzumab deruxtecan", "enfortumab vedotin",
    "sacituzumab govitecan", "brentuximab vedotin", "polatuzumab vedotin",
    "blinatumomab", "tisagenlecleucel", "axicabtagene", "idecabtagene",
    "aldesleukin", "interferon alfa", "peginterferon", "bcg intravesical",
    "radium-223", "lutetium-177", "iodine-131", "samarium-153",
}

# Supportive oncology: in scope, but on its own these are general hospital
# medicines. They carry a lower weight so "ondansetron ampoules" alone does
# not become an oncology tender, while "ondansetron for day care oncology"
# does.
SUPPORTIVE_ONCOLOGY = {
    "ondansetron", "granisetron", "palonosetron", "aprepitant", "fosaprepitant",
    "netupitant", "dexrazoxane", "mesna", "amifostine", "rasburicase",
    "filgrastim", "pegfilgrastim", "lenograstim", "epoetin", "darbepoetin",
    "zoledronic acid", "pamidronate", "denosumab", "calcium folinate",
    "folinic acid", "leucovorin", "levoleucovorin", "megestrol acetate",
    "medroxyprogesterone", "cytotoxic waste", "extravasation kit",
}

# WHO INN stems. A token ending in one of these, in a medicine context, is
# almost certainly an antineoplastic. This is what future-proofs the classifier.
ONCOLOGY_SUFFIXES = (
    "tinib",     # kinase inhibitors: osimertinib, imatinib, ibrutinib...
    "ciclib",    # CDK4/6: palbociclib, ribociclib, abemaciclib
    "parib",     # PARP: olaparib, niraparib, talazoparib
    "zomib",     # proteasome: bortezomib, carfilzomib, ixazomib
    "lisib",     # PI3K: alpelisib, idelalisib
    "rafenib",   # RAF: sorafenib, regorafenib
    "metinib",   # MEK: trametinib, binimetinib
    "degib",     # hedgehog: vismodegib, sonidegib
    "denib",     # IDH: ivosidenib, enasidenib
    "tecan",     # topoisomerase I: irinotecan, topotecan, deruxtecan
    "rubicin",   # anthracyclines
    "platin",    # platinums
    "taxel",     # taxanes
    "citabine",  # antimetabolites: gemcitabine, capecitabine, decitabine
    "vedotin",   # ADC payload linker
    "mertansine",
    "govitecan",
)

# -mab and -ximab need care: many monoclonals are not oncology (adalimumab,
# omalizumab). Only treat a -mab as oncology when it is a known oncology mab
# or appears alongside oncology context.
ONCOLOGY_MABS = {
    "rituximab", "trastuzumab", "pertuzumab", "bevacizumab", "cetuximab",
    "panitumumab", "ramucirumab", "ipilimumab", "nivolumab", "pembrolizumab",
    "atezolizumab", "durvalumab", "avelumab", "cemiplimab", "dostarlimab",
    "tremelimumab", "obinutuzumab", "ofatumumab", "daratumumab", "isatuximab",
    "elotuzumab", "mogamulizumab", "tafasitamab", "amivantamab", "teclistamab",
    "talquetamab", "mosunetuzumab", "glofitamab", "epcoritamab", "tarlatamab",
    "dinutuximab", "necitumumab", "olaratumab", "margetuximab", "naxitamab",
    "loncastuximab", "belantamab", "tisotumab", "mirvetuximab",
}

# --------------------------------------------------------------------------
# 3. Therapeutic-area classification (checked in order; first match wins,
#    except oncology which is decided by the dedicated oncology scorer)
# --------------------------------------------------------------------------

THERAPEUTIC_AREAS: dict[str, set[str]] = {
    "Hematology": {
        "haemophilia", "hemophilia", "factor viii", "factor ix", "von willebrand",
        "sickle cell", "thalassaemia", "thalassemia", "deferasirox", "deferiprone",
        "deferoxamine", "eltrombopag", "romiplostim", "itp", "aplastic anaemia",
        "anticoagulant", "warfarin", "enoxaparin", "heparin", "rivaroxaban",
        "apixaban", "dabigatran", "edoxaban", "eculizumab", "ravulizumab",
        "emicizumab", "caplacizumab", "هيموفيليا", "ثلاسيميا", "فقر الدم المنجلي",
    },
    "Immunology": {
        "rheumatoid arthritis", "psoriasis", "psoriatic", "ankylosing spondylitis",
        "crohn", "ulcerative colitis", "inflammatory bowel", "lupus",
        "adalimumab", "etanercept", "infliximab", "golimumab", "certolizumab",
        "tocilizumab", "secukinumab", "ustekinumab", "ixekizumab", "guselkumab",
        "risankizumab", "vedolizumab", "abatacept", "anakinra", "upadacitinib",
        "tofacitinib", "baricitinib", "azathioprine", "mycophenolate",
        "tacrolimus", "ciclosporin", "cyclosporine", "immunosuppressant",
        "روماتويد", "صدفية", "مناعة ذاتية",
    },
    "Rare Disease": {
        "orphan drug", "rare disease", "enzyme replacement", "gaucher",
        "fabry", "pompe", "mucopolysaccharidosis", "mps", "hunter syndrome",
        "spinal muscular atrophy", "nusinersen", "risdiplam", "onasemnogene",
        "duchenne", "cystic fibrosis", "ivacaftor", "lumacaftor", "elexacaftor",
        "phenylketonuria", "pku", "urea cycle", "wilson disease",
        "hereditary angioedema", "icatibant", "lanadelumab", "alglucosidase",
        "imiglucerase", "agalsidase", "laronidase", "idursulfase",
        "أمراض نادرة", "ضمور عضلي",
    },
    "Neurology": {
        "multiple sclerosis", "epilepsy", "antiepileptic", "parkinson",
        "alzheimer", "dementia", "migraine", "myasthenia gravis", "neuropathic",
        "levetiracetam", "lamotrigine", "valproate", "carbamazepine",
        "levodopa", "pramipexole", "rasagiline", "donepezil", "memantine",
        "natalizumab", "ocrelizumab", "fingolimod", "dimethyl fumarate",
        "teriflunomide", "interferon beta", "glatiramer", "erenumab",
        "galcanezumab", "fremanezumab", "botulinum toxin",
        "تصلب متعدد", "صرع", "باركنسون", "الزهايمر", "صداع نصفي",
    },
    "Cardiology": {
        "hypertension", "heart failure", "antihypertensive", "statin",
        "dyslipidaemia", "dyslipidemia", "atrial fibrillation", "angina",
        "atorvastatin", "rosuvastatin", "simvastatin", "amlodipine",
        "bisoprolol", "metoprolol", "carvedilol", "lisinopril", "ramipril",
        "valsartan", "losartan", "candesartan", "sacubitril", "ivabradine",
        "furosemide", "spironolactone", "clopidogrel", "ticagrelor",
        "evolocumab", "alirocumab", "inclisiran", "digoxin", "amiodarone",
        "ضغط الدم", "قصور القلب", "الكوليسترول",
    },
    "Endocrinology": {
        "diabetes", "insulin", "antidiabetic", "thyroid", "osteoporosis",
        "growth hormone", "somatropin", "metformin", "gliclazide", "glimepiride",
        "sitagliptin", "vildagliptin", "linagliptin", "empagliflozin",
        "dapagliflozin", "canagliflozin", "liraglutide", "semaglutide",
        "dulaglutide", "exenatide", "tirzepatide", "levothyroxine",
        "carbimazole", "methimazole", "alendronate", "teriparatide",
        "hydrocortisone", "prednisolone", "dexamethasone", "fludrocortisone",
        "سكري", "انسولين", "غدة درقية", "هشاشة العظام",
    },
    "Infectious Disease": {
        "antibiotic", "antibacterial", "antifungal", "antiviral", "antimalarial",
        "tuberculosis", "hiv", "antiretroviral", "hepatitis b", "hepatitis c",
        "meropenem", "imipenem", "piperacillin", "tazobactam", "vancomycin",
        "linezolid", "daptomycin", "ceftriaxone", "ceftazidime", "cefepime",
        "colistin", "tigecycline", "amikacin", "gentamicin", "levofloxacin",
        "ciprofloxacin", "azithromycin", "amoxicillin", "clavulanate",
        "fluconazole", "voriconazole", "posaconazole", "caspofungin",
        "amphotericin", "acyclovir", "valganciclovir", "oseltamivir",
        "remdesivir", "sofosbuvir", "ledipasvir", "dolutegravir", "tenofovir",
        "emtricitabine", "bictegravir", "isoniazid", "rifampicin",
        "vaccine", "لقاح", "مضاد حيوي", "مضاد فيروسي", "سل", "التهاب الكبد",
    },
    "Respiratory": {
        "asthma", "copd", "bronchodilator", "inhaler", "salbutamol",
        "budesonide", "formoterol", "fluticasone", "salmeterol", "tiotropium",
        "montelukast", "omalizumab", "mepolizumab", "benralizumab", "dupilumab",
        "ربو", "انسداد رئوي",
    },
    "Gastroenterology": {
        "proton pump inhibitor", "omeprazole", "esomeprazole", "pantoprazole",
        "lansoprazole", "mesalazine", "ursodeoxycholic", "lactulose",
        "rifaximin", "loperamide", "domperidone", "metoclopramide",
    },
}

# Products that are pharmaceutical but generic in nature -> Low priority.
COMMODITY_TERMS = {
    "sodium chloride", "dextrose", "ringer", "water for injection",
    "paracetamol", "acetaminophen", "ibuprofen", "diclofenac", "aspirin",
    "vitamin", "multivitamin", "folic acid", "ferrous", "calcium carbonate",
    "normal saline", "iv fluids", "antiseptic", "povidone iodine",
    "chlorhexidine", "alcohol swab", "glycerin", "paraffin",
}


# --------------------------------------------------------------------------
# 4. Folded index
# --------------------------------------------------------------------------
# Every lexicon above is written in natural orthography (Arabic with hamza,
# English with hyphens). Matching happens on folded text, so the lexicons are
# folded once at import. Short terms -- abbreviations like ALL, AML, GIST --
# are matched on word boundaries, because "installation" contains "all" and
# "capital" contains "api".

import re as _re
from .normalize import fold as _fold

SHORT_TERM_LEN = 5
_ARABIC_RX = _re.compile(r"[\u0600-\u06FF]")


def build_index(terms):
    """Return (substring_terms, compiled_word_boundary_regex_or_None)."""
    folded = {_fold(t) for t in terms}
    folded.discard("")
    # Arabic is always matched as a substring: the definite article "al-"
    # attaches to the noun (الأورام = al-awram), so a left word boundary would
    # never match, and Arabic has none of the short-abbreviation collisions
    # ("all", "api") that make boundaries necessary in English.
    arabic = {t for t in folded if _ARABIC_RX.search(t)}
    short = {t for t in folded - arabic if len(t) <= SHORT_TERM_LEN}
    long_ = folded - short
    rx = None
    if short:
        rx = _re.compile(
            r"(?<![\w\u0600-\u06FF])(?:"
            + "|".join(_re.escape(t) for t in sorted(short, key=len, reverse=True))
            + r")(?![\w\u0600-\u06FF])"
        )
    return long_, rx


INDEX = {
    name: build_index(terms)
    for name, terms in {
        "pharma": PHARMA_TERMS,
        "non_pharma": NON_PHARMA_TERMS,
        "device": DEVICE_TERMS,
        "onc_context": ONCOLOGY_CONTEXT,
        "onc_indication": CANCER_INDICATIONS,
        "onc_molecule": ONCOLOGY_MOLECULES,
        "onc_mab": ONCOLOGY_MABS,
        "onc_supportive": SUPPORTIVE_ONCOLOGY,
        "commodity": COMMODITY_TERMS,
    }.items()
}
AREA_INDEX = {area: build_index(terms) for area, terms in THERAPEUTIC_AREAS.items()}
