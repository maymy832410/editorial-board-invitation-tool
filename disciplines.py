"""Discipline categorization based on OpenAlex topics/subfields."""

# Mapping of OpenAlex subfield IDs to broad disciplines
# Based on https://api.openalex.org/subfields
SUBFIELD_TO_DISCIPLINE = {
    # Computer Science
    "1702": "Computer Science",  # Artificial Intelligence
    "1703": "Computer Science",  # Computational Theory and Mathematics
    "1704": "Computer Science",  # Computer Graphics and Computer-Aided Design
    "1705": "Computer Science",  # Computer Networks and Communications
    "1706": "Computer Science",  # Computer Science Applications
    "1707": "Computer Science",  # Computer Vision and Pattern Recognition
    "1708": "Computer Science",  # Hardware and Architecture
    "1709": "Computer Science",  # Human-Computer Interaction
    "1710": "Computer Science",  # Information Systems
    "1711": "Computer Science",  # Signal Processing
    "1712": "Computer Science",  # Software
    
    # Medicine & Healthcare
    "2700": "Medicine",  # General Medicine
    "2701": "Medicine",  # Medicine (miscellaneous)
    "2702": "Medicine",  # Anatomy
    "2703": "Medicine",  # Anesthesiology and Pain Medicine
    "2704": "Medicine",  # Biochemistry (medical)
    "2705": "Medicine",  # Cardiology and Cardiovascular Medicine
    "2706": "Medicine",  # Critical Care and Intensive Care Medicine
    "2707": "Medicine",  # Complementary and Manual Therapy
    "2708": "Medicine",  # Dermatology
    "2709": "Medicine",  # Emergency Medicine
    "2710": "Medicine",  # Endocrinology, Diabetes and Metabolism
    "2711": "Medicine",  # Epidemiology
    "2712": "Medicine",  # Family Practice
    "2713": "Medicine",  # Gastroenterology
    "2714": "Medicine",  # Genetics (clinical)
    "2715": "Medicine",  # Geriatrics and Gerontology
    "2716": "Medicine",  # Health Informatics
    "2717": "Medicine",  # Health Policy
    "2718": "Medicine",  # Hematology
    "2719": "Medicine",  # Hepatology
    "2720": "Medicine",  # Immunology and Allergy
    "2721": "Medicine",  # Infectious Diseases
    "2722": "Medicine",  # Internal Medicine
    "2723": "Medicine",  # Microbiology (medical)
    "2724": "Medicine",  # Nephrology
    "2725": "Medicine",  # Neurology (clinical)
    "2726": "Medicine",  # Obstetrics and Gynecology
    "2727": "Medicine",  # Oncology
    "2728": "Medicine",  # Ophthalmology
    "2729": "Medicine",  # Orthopedics and Sports Medicine
    "2730": "Medicine",  # Otorhinolaryngology
    "2731": "Medicine",  # Pathology and Forensic Medicine
    "2732": "Medicine",  # Pediatrics, Perinatology and Child Health
    "2733": "Medicine",  # Pharmacology (medical)
    "2734": "Medicine",  # Physiology (medical)
    "2735": "Medicine",  # Pediatrics, Perinatology and Child Health
    "2736": "Medicine",  # Psychiatry and Mental health
    "2737": "Medicine",  # Public Health, Environmental and Occupational Health
    "2738": "Medicine",  # Pulmonary and Respiratory Medicine
    "2739": "Medicine",  # Public Health, Environmental and Occupational Health
    "2740": "Medicine",  # Radiology Nuclear Medicine and Imaging
    "2741": "Medicine",  # Rehabilitation
    "2742": "Medicine",  # Reproductive Medicine
    "2743": "Medicine",  # Reviews and References (medical)
    "2744": "Medicine",  # Rheumatology
    "2745": "Medicine",  # Surgery
    "2746": "Medicine",  # Surgery
    "2747": "Medicine",  # Transplantation
    "2748": "Medicine",  # Urology
    
    # Biology
    "1100": "Biology",  # General Agricultural and Biological Sciences
    "1101": "Biology",  # Agricultural and Biological Sciences (miscellaneous)
    "1102": "Biology",  # Agronomy and Crop Science
    "1103": "Biology",  # Animal Science and Zoology
    "1104": "Biology",  # Aquatic Science
    "1105": "Biology",  # Ecology, Evolution, Behavior and Systematics
    "1106": "Biology",  # Food Science
    "1107": "Biology",  # Forestry
    "1108": "Biology",  # Horticulture
    "1109": "Biology",  # Insect Science
    "1110": "Biology",  # Plant Science
    "1111": "Biology",  # Soil Science
    
    # Biochemistry & Molecular Biology
    "1300": "Biochemistry",  # General Biochemistry, Genetics and Molecular Biology
    "1301": "Biochemistry",  # Biochemistry, Genetics and Molecular Biology (miscellaneous)
    "1302": "Biochemistry",  # Aging
    "1303": "Biochemistry",  # Biochemistry
    "1304": "Biochemistry",  # Biophysics
    "1305": "Biochemistry",  # Biotechnology
    "1306": "Biochemistry",  # Cancer Research
    "1307": "Biochemistry",  # Cell Biology
    "1308": "Biochemistry",  # Clinical Biochemistry
    "1309": "Biochemistry",  # Developmental Biology
    "1310": "Biochemistry",  # Endocrinology
    "1311": "Biochemistry",  # Genetics
    "1312": "Biochemistry",  # Molecular Biology
    "1313": "Biochemistry",  # Molecular Medicine
    "1314": "Biochemistry",  # Physiology
    "1315": "Biochemistry",  # Structural Biology
    
    # Chemistry
    "1500": "Chemistry",  # General Chemical Engineering
    "1501": "Chemistry",  # Chemical Engineering (miscellaneous)
    "1502": "Chemistry",  # Bioengineering
    "1503": "Chemistry",  # Catalysis
    "1504": "Chemistry",  # Chemical Health and Safety
    "1505": "Chemistry",  # Colloid and Surface Chemistry
    "1506": "Chemistry",  # Filtration and Separation
    "1507": "Chemistry",  # Fluid Flow and Transfer Processes
    "1508": "Chemistry",  # Process Chemistry and Technology
    "2500": "Chemistry",  # General Chemistry
    "2501": "Chemistry",  # Chemistry (miscellaneous)
    "2502": "Chemistry",  # Analytical Chemistry
    "2503": "Chemistry",  # Electrochemistry
    "2504": "Chemistry",  # Inorganic Chemistry
    "2505": "Chemistry",  # Materials Chemistry
    "2506": "Chemistry",  # Organic Chemistry
    "2507": "Chemistry",  # Physical and Theoretical Chemistry
    "2508": "Chemistry",  # Spectroscopy
    
    # Physics
    "3100": "Physics",  # General Physics and Astronomy
    "3101": "Physics",  # Physics and Astronomy (miscellaneous)
    "3102": "Physics",  # Acoustics and Ultrasonics
    "3103": "Physics",  # Astronomy and Astrophysics
    "3104": "Physics",  # Condensed Matter Physics
    "3105": "Physics",  # Instrumentation
    "3106": "Physics",  # Nuclear and High Energy Physics
    "3107": "Physics",  # Atomic and Molecular Physics, and Optics
    "3108": "Physics",  # Radiation
    "3109": "Physics",  # Statistical and Nonlinear Physics
    "3110": "Physics",  # Surfaces and Interfaces
    
    # Engineering
    "2200": "Engineering",  # General Engineering
    "2201": "Engineering",  # Engineering (miscellaneous)
    "2202": "Engineering",  # Aerospace Engineering
    "2203": "Engineering",  # Automotive Engineering
    "2204": "Engineering",  # Biomedical Engineering
    "2205": "Engineering",  # Civil and Structural Engineering
    "2206": "Engineering",  # Computational Mechanics
    "2207": "Engineering",  # Control and Systems Engineering
    "2208": "Engineering",  # Electrical and Electronic Engineering
    "2209": "Engineering",  # Industrial and Manufacturing Engineering
    "2210": "Engineering",  # Mechanical Engineering
    "2211": "Engineering",  # Mechanics of Materials
    "2212": "Engineering",  # Ocean Engineering
    "2213": "Engineering",  # Safety, Risk, Reliability and Quality
    
    # Mathematics
    "2600": "Mathematics",  # General Mathematics
    "2601": "Mathematics",  # Mathematics (miscellaneous)
    "2602": "Mathematics",  # Algebra and Number Theory
    "2603": "Mathematics",  # Analysis
    "2604": "Mathematics",  # Applied Mathematics
    "2605": "Mathematics",  # Computational Mathematics
    "2606": "Mathematics",  # Control and Optimization
    "2607": "Mathematics",  # Discrete Mathematics and Combinatorics
    "2608": "Mathematics",  # Geometry and Topology
    "2609": "Mathematics",  # Logic
    "2610": "Mathematics",  # Mathematical Physics
    "2611": "Mathematics",  # Modeling and Simulation
    "2612": "Mathematics",  # Numerical Analysis
    "2613": "Mathematics",  # Statistics and Probability
    "2614": "Mathematics",  # Theoretical Computer Science
    
    # Economics & Business
    "2000": "Economics",  # General Economics, Econometrics and Finance
    "2001": "Economics",  # Economics, Econometrics and Finance (miscellaneous)
    "2002": "Economics",  # Economics and Econometrics
    "2003": "Economics",  # Finance
    "1400": "Business",  # General Business, Management and Accounting
    "1401": "Business",  # Business, Management and Accounting (miscellaneous)
    "1402": "Business",  # Accounting
    "1403": "Business",  # Business and International Management
    "1404": "Business",  # Management Information Systems
    "1405": "Business",  # Management of Technology and Innovation
    "1406": "Business",  # Marketing
    "1407": "Business",  # Organizational Behavior and Human Resource Management
    "1408": "Business",  # Strategy and Management
    "1409": "Business",  # Tourism, Leisure and Hospitality Management
    "1410": "Business",  # Industrial Relations
    
    # Psychology
    "3200": "Psychology",  # General Psychology
    "3201": "Psychology",  # Psychology (miscellaneous)
    "3202": "Psychology",  # Applied Psychology
    "3203": "Psychology",  # Clinical Psychology
    "3204": "Psychology",  # Developmental and Educational Psychology
    "3205": "Psychology",  # Experimental and Cognitive Psychology
    "3206": "Psychology",  # Neuropsychology and Physiological Psychology
    "3207": "Psychology",  # Social Psychology
    
    # Social Sciences
    "3300": "Social Sciences",  # General Social Sciences
    "3301": "Social Sciences",  # Social Sciences (miscellaneous)
    "3302": "Social Sciences",  # Archaeology
    "3303": "Social Sciences",  # Development
    "3304": "Social Sciences",  # Education
    "3305": "Social Sciences",  # Geography, Planning and Development
    "3306": "Social Sciences",  # Health (social science)
    "3307": "Social Sciences",  # Human Factors and Ergonomics
    "3308": "Social Sciences",  # Law
    "3309": "Social Sciences",  # Library and Information Sciences
    "3310": "Social Sciences",  # Linguistics and Language
    "3311": "Social Sciences",  # Safety Research
    "3312": "Social Sciences",  # Sociology and Political Science
    "3313": "Social Sciences",  # Transportation
    "3314": "Social Sciences",  # Anthropology
    "3315": "Social Sciences",  # Communication
    "3316": "Social Sciences",  # Cultural Studies
    "3317": "Social Sciences",  # Demography
    "3318": "Social Sciences",  # Gender Studies
    "3319": "Social Sciences",  # Life-span and Life-course Studies
    "3320": "Social Sciences",  # Political Science and International Relations
    "3321": "Social Sciences",  # Public Administration
    "3322": "Social Sciences",  # Urban Studies
    
    # Environmental Science
    "2300": "Environmental Science",  # General Environmental Science
    "2301": "Environmental Science",  # Environmental Science (miscellaneous)
    "2302": "Environmental Science",  # Ecological Modeling
    "2303": "Environmental Science",  # Ecology
    "2304": "Environmental Science",  # Environmental Chemistry
    "2305": "Environmental Science",  # Environmental Engineering
    "2306": "Environmental Science",  # Global and Planetary Change
    "2307": "Environmental Science",  # Health, Toxicology and Mutagenesis
    "2308": "Environmental Science",  # Management, Monitoring, Policy and Law
    "2309": "Environmental Science",  # Nature and Landscape Conservation
    "2310": "Environmental Science",  # Pollution
    "2311": "Environmental Science",  # Waste Management and Disposal
    "2312": "Environmental Science",  # Water Science and Technology
    
    # Earth Sciences
    "1900": "Earth Sciences",  # General Earth and Planetary Sciences
    "1901": "Earth Sciences",  # Earth and Planetary Sciences (miscellaneous)
    "1902": "Earth Sciences",  # Atmospheric Science
    "1903": "Earth Sciences",  # Computers in Earth Sciences
    "1904": "Earth Sciences",  # Earth-Surface Processes
    "1905": "Earth Sciences",  # Economic Geology
    "1906": "Earth Sciences",  # Geochemistry and Petrology
    "1907": "Earth Sciences",  # Geology
    "1908": "Earth Sciences",  # Geophysics
    "1909": "Earth Sciences",  # Geotechnical Engineering and Engineering Geology
    "1910": "Earth Sciences",  # Oceanography
    "1911": "Earth Sciences",  # Paleontology
    "1912": "Earth Sciences",  # Space and Planetary Science
    "1913": "Earth Sciences",  # Stratigraphy
    
    # Arts & Humanities
    "1200": "Arts & Humanities",  # General Arts and Humanities
    "1201": "Arts & Humanities",  # Arts and Humanities (miscellaneous)
    "1202": "Arts & Humanities",  # History
    "1203": "Arts & Humanities",  # Language and Linguistics
    "1204": "Arts & Humanities",  # Archeology
    "1205": "Arts & Humanities",  # Classics
    "1206": "Arts & Humanities",  # Conservation
    "1207": "Arts & Humanities",  # History and Philosophy of Science
    "1208": "Arts & Humanities",  # Literature and Literary Theory
    "1209": "Arts & Humanities",  # Museology
    "1210": "Arts & Humanities",  # Music
    "1211": "Arts & Humanities",  # Philosophy
    "1212": "Arts & Humanities",  # Religious Studies
    "1213": "Arts & Humanities",  # Visual Arts and Performing Arts
    
    # Materials Science
    "2500": "Materials Science",  # General Materials Science
    "2501": "Materials Science",  # Materials Science (miscellaneous)
    "2502": "Materials Science",  # Biomaterials
    "2503": "Materials Science",  # Ceramics and Composites
    "2504": "Materials Science",  # Electronic, Optical and Magnetic Materials
    "2505": "Materials Science",  # Materials Chemistry
    "2506": "Materials Science",  # Metals and Alloys
    "2507": "Materials Science",  # Polymers and Plastics
    "2508": "Materials Science",  # Surfaces, Coatings and Films
}

# List of all disciplines for UI
ALL_DISCIPLINES = sorted(list(set(SUBFIELD_TO_DISCIPLINE.values())))


def get_discipline_from_topics(topics: list) -> str:
    """
    Determine the primary discipline from author's topics.
    
    Args:
        topics: List of topic dicts from OpenAlex author record
        
    Returns:
        Primary discipline name or "Other"
    """
    if not topics:
        return "Other"
    
    # Count disciplines from topics
    discipline_counts = {}
    
    for topic in topics:
        subfield = topic.get("subfield", {})
        subfield_id = subfield.get("id", "")
        
        # Extract numeric ID from URL
        if "/" in subfield_id:
            subfield_id = subfield_id.split("/")[-1]
        
        discipline = SUBFIELD_TO_DISCIPLINE.get(subfield_id, "Other")
        discipline_counts[discipline] = discipline_counts.get(discipline, 0) + 1
    
    # Return the most common discipline (excluding "Other" if possible)
    if discipline_counts:
        # Sort by count, prefer non-Other
        sorted_disciplines = sorted(
            discipline_counts.items(),
            key=lambda x: (x[0] != "Other", x[1]),
            reverse=True
        )
        return sorted_disciplines[0][0]
    
    return "Other"


def categorize_authors(authors: list) -> list:
    """
    Add discipline field to each author based on their topics.
    
    Args:
        authors: List of author dicts
        
    Returns:
        Same list with 'discipline' field added to each author
    """
    for author in authors:
        # Get topics from the raw data if available
        topics = author.get("_topics", [])
        author["discipline"] = get_discipline_from_topics(topics)
    
    return authors
