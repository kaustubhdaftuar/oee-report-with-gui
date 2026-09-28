"""
============================================================
OEE WEEKLY MANAGEMENT REPORT — GEMMA 2 2B
============================================================

Purpose:
Generate concise, factual AI narrative for the weekly OEE
management report.

SOURCE OF TRUTH:

analysis.json
    -> verified numerical data

Gemma
    -> narrative interpretation only

insights.json
    -> stores the generated AI narrative

template.docx
    -> layout only

CRITICAL:
The model must NEVER invent numbers, causes, events,
maintenance findings, or unsupported recommendations.
============================================================
"""


# ============================================================
# SYSTEM PROMPT
# ============================================================

REPORT_SYSTEM_PROMPT = r"""
You are an industrial OEE reporting analyst for a cement
plant packer line.

Your job is to explain VERIFIED OEE DATA supplied by Python.

You MUST follow these rules:

============================================================
1. DATA INTEGRITY
============================================================

The supplied data is the ONLY source of truth.

You may:

- repeat supplied numbers
- subtract current and previous KPI values
- rank supplied loss categories
- calculate percentages from supplied totals
- identify the weakest KPI
- identify the strongest KPI
- describe production gaps
- describe quality losses
- describe supplied shift-changeover delays

You MUST NOT:

- invent causes
- invent maintenance findings
- invent machine failures
- invent operator problems
- invent events
- invent production quantities
- invent downtime
- invent quality losses
- invent targets
- invent historical values
- assume a mechanical or electrical root cause
- recommend replacing a component unless the supplied
  data explicitly proves that component is the cause

If something is not supplied, write that the data was not
supplied.

============================================================
2. NUMBERS
============================================================

Use the supplied numerical values exactly.

Percentages:
64.14%

Minutes:
100.00 minutes

Bags:
20,000 bags

Percentage-point changes:
5.00 percentage points

Do NOT confuse percentage points with relative percentage
change.

Example:

80% -> 75%

Correct:
decreased by 5.00 percentage points

Do NOT write:
decreased by 6.25%

unless relative percentage change is explicitly requested.

============================================================
3. KPI RANKING
============================================================

Compare ONLY:

- Availability
- Performance
- Quality

Do NOT use OEE as the weakest or strongest component KPI.

The weakest KPI is the lowest of Availability,
Performance and Quality.

The strongest KPI is the highest of Availability,
Performance and Quality.

If two or more are tied, name all tied KPIs.

============================================================
4. OEE
============================================================

Use the supplied OEE value.

Compare OEE with the supplied Target OEE.

If Target OEE is not supplied, state that target data was
not supplied.

Do not invent an OEE target.

============================================================
5. PERFORMANCE
============================================================

Use:

- Actual good bags
- Expected good bags
- Production gap

The production gap is:

Expected good bags - Actual good bags

Do not invent reasons for the production gap.

============================================================
6. DOWNTIME
============================================================

Use ONLY the supplied downtime contributors.

Rank them from largest to smallest.

The first listed contributor is the main loss driver.

You may calculate:

contributor minutes / total classified downtime * 100

Do not invent a root cause.

For example:

Correct:
"Fillpac 4 Belt Not Running accounts for 178.81 minutes
of classified downtime."

Incorrect:
"The belt failed because of mechanical wear."

The second statement is not allowed unless the supplied
data explicitly proves it.

============================================================
7. SHIFT CHANGEOVER
============================================================

If shift changeover data is supplied, mention it only when
relevant.

Use the supplied values.

For example:

"Shift C recorded 207.03 minutes of changeover delay."

Do not invent the reason for the delay.

============================================================
8. QUALITY
============================================================

Use ONLY supplied quality data.

Quality loss may include:

- Burst Bags
- Out of Limit Bags
- other explicitly supplied reject reasons

Rank supplied quality-loss contributors from largest to
smallest.

Do not invent quality causes.

For example:

Correct:
"Out of Limit Bags account for 20 bags of recorded quality
loss."

Incorrect:
"The bags were caused by incorrect calibration."

unless calibration is explicitly supplied as the cause.

============================================================
9. WEEK-OVER-WEEK TREND
============================================================

Compare current week with previous week.

For each supplied KPI state:

- increased
- decreased
- remained unchanged

Use percentage points.

Example:

"Performance decreased by 4.00 percentage points from
70.00% to 66.00%."

If previous-week data is unavailable, state that it was not
supplied.

============================================================
10. MISSING DATA
============================================================

Use MISSING DATA to identify information that is genuinely
absent and relevant.

Do not claim data is missing if it is actually supplied.

If no important data is missing, write:

"No material data gaps identified."

============================================================
11. RECOMMENDATIONS
============================================================

Recommendations must be directly supported by supplied data.

Allowed:

"Prioritize investigation of the largest recorded downtime
driver."

Not allowed:

"Replace the motor bearing."

unless the supplied data explicitly identifies the motor
bearing as the cause.

============================================================
12. STYLE
============================================================

Use:

- simple English
- concise sentences
- management-friendly language
- factual wording
- direct statements

Avoid:

- dramatic language
- speculation
- unsupported root causes
- unnecessary technical jargon
- long explanations

============================================================
OUTPUT REQUIREMENT
============================================================

Return EXACTLY the following nine sections.

Do not add text before the first section.

Do not add text after the final section.

Do not use JSON.

Do not use markdown.

Do not use bullet points.

Do not use code fences.

Required headers must be written exactly as shown:

EXECUTIVE SUMMARY:

MAIN LOSS DRIVER:

PERFORMANCE CONCERN:

STRONGEST KPI:

WEEK-OVER-WEEK TREND:

QUALITY SUMMARY:

MAIN QUALITY ISSUE:

KEY DECISION POINT:

MISSING DATA:
"""


# ============================================================
# REPORT INSTRUCTIONS
# ============================================================

REPORT_INSTRUCTIONS = r"""
Generate the weekly OEE management narrative using ONLY the
VERIFIED DATA supplied below.

Required output:

EXECUTIVE SUMMARY:
Summarize the current OEE, target comparison if target is
supplied, weakest component KPI and strongest component KPI.

MAIN LOSS DRIVER:
Identify the largest supplied downtime contributor.
Include its minutes and percentage of classified downtime
when the denominator is available.

PERFORMANCE CONCERN:
Explain the supplied Performance value and actual-vs-expected
production gap. Do not invent a reason for the gap.

STRONGEST KPI:
Identify the strongest component KPI among Availability,
Performance and Quality. Include its supplied value.

WEEK-OVER-WEEK TREND:
Describe Availability, Performance and Quality changes
versus the previous week using percentage points.

QUALITY SUMMARY:
Summarize the current Quality value and supplied quality-loss
data.

MAIN QUALITY ISSUE:
Identify the largest supplied quality-loss contributor.
Include bags and percentage of recorded quality loss when
computable.

KEY DECISION POINT:
State the single most important priority based on the supplied
data. Tie the priority to the weakest KPI or largest verified
loss. Do not invent a root cause.

MISSING DATA:
List relevant information that was not supplied. If nothing
material is missing, write:
No material data gaps identified.

Remember:

analysis.json is the numerical source of truth.

Do not invent facts.

Do not invent causes.

Do not invent maintenance findings.

Do not invent events.

Do not invent numbers.

Return ONLY the nine required sections.
"""


# ============================================================
# BUILD PROMPT
# ============================================================

def build_prompt(
    system_prompt: str,
    user_data: str,
) -> str:
    """
    Combine the system instructions and verified data.

    This signature intentionally matches local_llm_client.py.
    """

    return (
        f"{system_prompt}\n\n"
        f"{REPORT_INSTRUCTIONS}\n\n"
        "============================================================\n"
        "VERIFIED DATA FROM PYTHON\n"
        "============================================================\n\n"
        f"{user_data}"
    )


# ============================================================
# SECTION EXTRACTION
# ============================================================

REQUIRED_SECTIONS = [

    "EXECUTIVE SUMMARY",

    "MAIN LOSS DRIVER",

    "PERFORMANCE CONCERN",

    "STRONGEST KPI",

    "WEEK-OVER-WEEK TREND",

    "QUALITY SUMMARY",

    "MAIN QUALITY ISSUE",

    "KEY DECISION POINT",

    "MISSING DATA",
]


def extract_sections(
    model_output: str,
) -> dict:
    """
    Compatibility parser for direct use of prompts.py.

    local_llm_client.py contains the primary parser.
    """

    sections = {
        section: ""
        for section
        in REQUIRED_SECTIONS
    }

    if not model_output:
        return sections

    text = (
        model_output
        .replace(
            "\r\n",
            "\n",
        )
        .replace(
            "\r",
            "\n",
        )
    )

    current_section = None

    lines = text.split(
        "\n"
    )

    for line in lines:

        clean = re.sub(
            r"^\s*#{1,6}\s*",
            "",
            line,
        ).strip()

        matched = False

        for header in REQUIRED_SECTIONS:

            if re.match(
                rf"^{re.escape(header)}\s*:?",
                clean,
                flags=re.IGNORECASE,
            ):

                current_section = header

                content = clean[
                    len(header):
                ].lstrip(
                    " :"
                ).strip()

                if content:

                    sections[
                        header
                    ] = content

                matched = True

                break

        if matched:
            continue

        if (
            current_section
            and clean
        ):

            if sections[
                current_section
            ]:

                sections[
                    current_section
                ] += " " + clean

            else:

                sections[
                    current_section
                ] = clean

    for key in sections:

        sections[key] = (
            " ".join(
                sections[key].split()
            )
        )

    return sections


# ============================================================
# DATA VALIDATION
# ============================================================

def validate_data_completeness(
    data_dict: dict,
) -> list:
    """
    Validate the legacy flat data format.

    This helper is retained for compatibility with older code.
    """

    required_fields = [

        "plant_site_name",

        "equipment_line_name",

        "report_week",

        "current_week_oee",

        "current_week_availability",

        "current_week_performance",

        "current_week_quality",

        "previous_week_oee",

        "previous_week_availability",

        "previous_week_performance",

        "previous_week_quality",

        "target_oee",

        "planned_production_time_min",

        "downtime_faults",

        "total_produced_bags",

        "good_bags",

        "rejected_bags",

        "quality_loss_reasons",
    ]

    return [

        field

        for field
        in required_fields

        if (
            field not in data_dict
            or data_dict[field] is None
        )
    ]


# ============================================================
# WEEK-OVER-WEEK
# ============================================================

def format_percentage_point_change(
    current: float,
    previous: float,
) -> tuple:
    """
    Return:

        (absolute_change, direction)
    """

    change = (
        current
        - previous
    )

    tolerance = 0.01

    if abs(change) < tolerance:

        direction = (
            "remained unchanged"
        )

    elif change > 0:

        direction = (
            "increased"
        )

    else:

        direction = (
            "decreased"
        )

    return (
        abs(change),
        direction,
    )


# ============================================================
# DOWNTIME RANKING
# ============================================================

def rank_fault_codes(
    downtime_faults: list,
) -> list:
    """
    Rank downtime causes by minutes lost.
    """

    ranked = sorted(
        downtime_faults,
        key=lambda item:
            item.get(
                "minutes",
                0,
            ),
        reverse=True,
    )

    for index, fault in enumerate(
        ranked,
        start=1,
    ):

        fault["rank"] = index

    return ranked


# ============================================================
# QUALITY RANKING
# ============================================================

def rank_quality_losses(
    quality_loss_reasons: list,
) -> list:
    """
    Rank quality-loss causes by bags lost.
    """

    ranked = sorted(
        quality_loss_reasons,
        key=lambda item:
            item.get(
                "bags",
                0,
            ),
        reverse=True,
    )

    for index, loss in enumerate(
        ranked,
        start=1,
    ):

        loss["rank"] = index

    return ranked


# ============================================================
# TOP DOWNTIME SHARE
# ============================================================

def calculate_top_downtime_share(
    downtime_faults: list,
    n_drivers: int = 3,
) -> float:
    """
    Calculate combined percentage of top N downtime drivers.
    """

    if not downtime_faults:
        return 0.0

    total_minutes = sum(
        float(
            item.get(
                "minutes",
                0,
            )
        )
        for item
        in downtime_faults
    )

    if total_minutes <= 0:
        return 0.0

    top_n_minutes = sum(
        float(
            item.get(
                "minutes",
                0,
            )
        )
        for item
        in downtime_faults[
            :n_drivers
        ]
    )

    return (
        top_n_minutes
        / total_minutes
        * 100
    )


# ============================================================
# TOP QUALITY LOSS SHARE
# ============================================================

def calculate_top_quality_loss_share(
    quality_loss_reasons: list,
    n_reasons: int = 3,
) -> float:
    """
    Calculate combined percentage of top N quality-loss causes.
    """

    if not quality_loss_reasons:
        return 0.0

    total_bags = sum(
        float(
            item.get(
                "bags",
                0,
            )
        )
        for item
        in quality_loss_reasons
    )

    if total_bags <= 0:
        return 0.0

    top_n_bags = sum(
        float(
            item.get(
                "bags",
                0,
            )
        )
        for item
        in quality_loss_reasons[
            :n_reasons
        ]
    )

    return (
        top_n_bags
        / total_bags
        * 100
    )


# ============================================================
# WEAKEST KPI
# ============================================================

def identify_weakest_kpi(
    availability: float,
    performance: float,
    quality: float,
) -> str:
    """
    Identify the weakest component KPI.
    """

    kpis = {

        "Availability":
            availability,

        "Performance":
            performance,

        "Quality":
            quality,
    }

    minimum = min(
        kpis.values()
    )

    weakest = [

        name

        for name, value
        in kpis.items()

        if abs(
            value - minimum
        ) < 0.01
    ]

    if len(weakest) == 1:

        return weakest[0]

    return " and ".join(
        weakest
    )


# ============================================================
# STRONGEST KPI
# ============================================================

def identify_strongest_kpi(
    availability: float,
    performance: float,
    quality: float,
) -> str:
    """
    Identify the strongest component KPI.
    """

    kpis = {

        "Availability":
            availability,

        "Performance":
            performance,

        "Quality":
            quality,
    }

    maximum = max(
        kpis.values()
    )

    strongest = [

        name

        for name, value
        in kpis.items()

        if abs(
            value - maximum
        ) < 0.01
    ]

    if len(strongest) == 1:

        return strongest[0]

    return " and ".join(
        strongest
    )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    print(
        "OEE Weekly Management Report "
        "prompt module"
    )

    print(
        "=" * 60
    )

    print(
        "Required AI sections:"
    )

    for section in REQUIRED_SECTIONS:

        print(
            f"  - {section}"
        )