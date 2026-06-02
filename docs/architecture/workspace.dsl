workspace "Consultant Opportunity Matching Platform — MVP 1.0" "Architecture model for the consultant opportunity matching and CV preparation platform. Designed using the C4 model. ADRs for key decisions are maintained separately in /adr." {

    !identifiers hierarchical

    model {

        # ─────────────────────────────────────────────
        # ACTORS
        # ─────────────────────────────────────────────

        consultant = person "Consultant" "The professional actively seeking contract or permanent positions. Reviews matched opportunities, gives verdicts, and initiates CV generation."

        talentAdvisor = person "Talent Advisor" "Configures the system (active consultants, job board sources). Receives notifications when a consultant accepts a position and when a CV is ready. Handles the human side of the hiring process."

        systemAdministrator = person "System Administrator" "Monitors system health. Receives failure alerts and reviews held configuration updates that exceed the bounded-delta threshold."

        # ─────────────────────────────────────────────
        # EXTERNAL SYSTEMS
        # ─────────────────────────────────────────────

        jobBoards = softwareSystem "Job Boards" "External job advertising platforms (e.g. LinkedIn, Jobtech/Arbetsförmedlingen, specialist boards). Source of raw position data." "External"

        llmProvider = softwareSystem "LLM Provider" "External large language model API (e.g. Anthropic Claude). Used for position standardization, matching configuration generation, experience extraction, and CV content generation." "External"

        # ─────────────────────────────────────────────
        # DATA LAYER
        # Shared infrastructure owned by neither pipeline.
        # Modelled as a separate system so containers have
        # a valid home in Structurizr without implying
        # pipeline ownership. Not shown as a box at L1.
        # ─────────────────────────────────────────────

        dataLayer = softwareSystem "Data Layer" "Shared persistent stores and observability infrastructure. Consumed by both pipelines. Not a user-facing system." "Internal" {

            jobStore = container "Job Store" "Stores all collected job positions with lifecycle status (active / expired). Default queries return active positions only. Expired positions are retained until Application Tracker references are resolved." "Database" "Database"

            profileConfigStore = container "Profile & Configuration Store" "Stores consultant profile data (experience, studies, interests) and the current matching configuration produced by the Learning Module. Two logical schemas; one container." "Database" "Database"

            applicationTracker = container "Application Tracker" "Records the full history of consultant verdicts (yes / no / maybe) and free-text reasoning, keyed by consultant and position. The Learning Module's primary input for configuration updates." "Database" "Database"

            taConfigStore = container "TA Configuration Store" "Stores Talent Advisor operational configuration: which job boards to collect from, and which consultants are currently active. Scoped per Talent Advisor." "Database" "Database"

            observability = container "Observability" "Receives logs, errors, and health signals from all containers. Queried by the System Administrator. Not a domain store — no container reads from it at runtime." "Logging & Monitoring" "Observability"
        }

        # ─────────────────────────────────────────────
        # OPPORTUNITY MATCHING PIPELINE
        # ─────────────────────────────────────────────

        opportunityMatchingSystem = softwareSystem "Opportunity Matching Pipeline" "Collects job positions daily, pre-selects the top 10 against the consultant profile, standardizes them with an LLM, presents them on the Dashboard, captures consultant verdicts, and uses those verdicts to improve future matching." "Internal" {

            sourceCollector = container "Source Collector" "Scheduled daily. Pulls raw job postings from configured job boards. Writes new positions to the Job Store. Triggered by an internal scheduler; no human trigger required." "Python" "Component" {

                scheduler = component "Scheduler" "Triggers the collection cycle daily. Configurable schedule. Emits a failure alert to Observability if the cycle does not complete within the expected window."
                boardConnector = component "Board Connector" "Reads active job board sources from the TA Configuration Store. Fetches raw postings from each configured board. Handles pagination and rate limiting per board."
                positionWriter = component "Position Writer" "Deduplicates incoming positions against existing Job Store records. Writes new positions with status=active and the board's stated close date. Emits a count summary to Observability."
            }

            preFilterMatcher = container "Pre-Filter & Matcher" "Code-based scoring component. Ranks all active positions in the Job Store against the consultant's current matching configuration. Selects the top 10 for standardization, including the exploration budget slots. Used for both pre-filtering and consultant-facing match scoring — the same component, the same configuration." "Python" "Component" {

                configReader = component "Configuration Reader" "Reads the current matching configuration for the consultant from the Profile & Configuration Store. Validates the configuration against the matching configuration schema before use. Rejects malformed configurations and falls back to the previous valid version."
                scoringEngine = component "Scoring Engine" "Applies the matching configuration to each active position. Computes a weighted score per position based on terms, weights, and thresholds defined in the configuration. Deterministic: same configuration + same positions = same scores."
                explorationSelector = component "Exploration Selector" "Reserves 1–2 slots of the top-10 selection for exploration positions — chosen by diversity criteria (role type, recency, random sampling from top 20) rather than pure score. Configurable budget size. Ensures the feedback loop cannot fully close around a narrow configuration."
                batchAssembler = component "Batch Assembler" "Combines the top scored positions with exploration selections to produce the final batch of 10. Passes the batch to the LLM Standardizer."
            }

            llmStandardizer = container "LLM Standardizer" "Sends the batch of 10 positions to the LLM Provider in a single batched call. Receives structured, human-readable descriptions for each position. Writes standardized descriptions back to the Job Store against their position records." "Python" "Component" {

                batchPromptBuilder = component "Batch Prompt Builder" "Constructs a single prompt containing all 10 positions and the standardization instructions. Batching is deliberate: one LLM call per cycle, not one per position."
                responseParser = component "Response Parser" "Parses the LLM's structured response. Validates that a description was returned for each input position. Writes descriptions to the Job Store. Emits a partial-failure alert to Observability if any position description is missing."
            }

            dashboard = container "Dashboard" "Presents the 10 standardized positions to the Consultant. Captures verdict (yes / no / maybe) and free-text reasoning for each position. Enforces that 'no' and 'maybe' verdicts cannot be submitted without a reason. Triggers the Learning Module when all verdicts for a cycle are complete." "Web Application" "WebBrowser" {

                positionRenderer = component "Position Renderer" "Reads standardized position descriptions from the Job Store. Renders each position with its LLM-produced description and the consultant's current match score."
                verdictCapture = component "Verdict Capture" "Renders the verdict form per position. Enforces the mandatory-reason constraint: the submit action is disabled for 'no' and 'maybe' until a non-empty reason is provided. Accepts short reasons; constrains presence, not length."
                cycleController = component "Cycle Controller" "Tracks verdict completion for the current batch. When all 10 positions have received verdicts, writes the full verdict set to the Application Tracker and triggers the Learning Module."
            }

            learningModule = container "Learning Module" "Activated at the end of each Dashboard Review cycle. Reads verdict history from the Application Tracker and the current configuration from the Profile & Configuration Store. Calls the LLM to produce an updated matching configuration. Applies schema validation and bounded-delta safeguards before writing the new configuration." "Python" "Component" {

                historyAggregator = component "History Aggregator" "Reads all verdict history for the consultant from the Application Tracker. Structures the history (position summaries, verdicts, free-text reasons, cycle dates) as input for the LLM prompt."
                configUpdatePromptBuilder = component "Config Update Prompt Builder" "Constructs the LLM prompt. Includes: the consultant's current profile, the current matching configuration, and the full structured verdict history. Instructs the LLM to emit a valid updated configuration conforming to the matching configuration schema."
                schemaValidator = component "Schema Validator" "Validates the LLM's response against the matching configuration schema. Rejects malformed output. On rejection: retains the previous configuration, emits an error to Observability, notifies the System Administrator."
                deltaGuard = component "Delta Guard" "Compares the validated new configuration to the current one. If changes exceed the bounded-delta threshold (configurable: e.g. weight shift > ±0.3 per term, or > N term removals), holds the configuration for manual review rather than applying it. Notifies the System Administrator."
                configWriter = component "Config Writer" "Writes the approved updated configuration to the Profile & Configuration Store. Tags it with the cycle identifier and timestamp for auditability."
            }
        }

        # ─────────────────────────────────────────────
        # CV PREPARATION PIPELINE
        # ─────────────────────────────────────────────

        cvPreparationSystem = softwareSystem "CV Preparation Pipeline" "Generates tailored CVs for consultant-selected positions. Accepts a (consultant, position) handoff from the Dashboard. Reads position data and consultant experience from shared stores. Notifies the Talent Advisor when a CV is ready." "Internal" {

            cvImporter = container "CV Importer" "Accepts CV file uploads from the Consultant. Extracts raw text, calls the LLM to structure it into experience entries conforming to the experience database schema, and writes the result to the Profile & Configuration Store." "Python / Web UI" "Component" {

                fileReceiver = component "File Receiver" "Accepts uploaded CV files from the Consultant. Validates format (PDF, DOCX). Rejects unsupported formats with a clear error. Passes validated files to the CV Parser."

                cvParser = component "CV Parser" "Extracts raw text from the uploaded file using a document parsing library. No LLM involvement — this is a deterministic extraction step. Passes raw text to the Experience Extractor."

                experienceExtractor = component "Experience Extractor" "Calls the LLM with the raw CV text and the experience database schema (experience-db.schema.md). Instructs the LLM to identify and structure experience entries into the defined fields and sections. Handles document-style input: expects structured sentences, multiple roles, date ranges."

                cvImporterExperienceWriter = component "Experience Writer" "Validates the LLM's structured output against the experience database schema. Rejects malformed entries and emits an error to Observability. Writes validated experience entries to the Profile & Configuration Store."
            }

            cvPipeline = container "CV Pipeline" "Orchestrates CV generation for a given (consultant, position) pair. Performs a validity check at entry, assembles context, generates CV content via a single LLM call, renders to HTML, and notifies the Talent Advisor. Also handles in-session experience capture from free-text consultant input." "Python" "Component" {

                handoffReceiver = component "Handoff Receiver" "Receives the (consultant, position) tuple from the Dashboard. Performs an immediate validity check against the Job Store: if the position is expired, emits an expiry notification to the Consultant and does not proceed. If valid, triggers the Context Assembler."

                contextAssembler = component "Context Assembler" "Reads all data needed for CV generation in a single pass: raw position text from the Job Store, and the full consultant experience database from the Profile & Configuration Store. Holds this context in working memory for the duration of the run. Does not re-query stores mid-run (per ADR-005)."

                cvContentGenerator = component "CV Content Generator" "Single LLM call: given the assembled context and the CV content schema (cv-content.schema.md), produces a filled CV content document. The LLM selects and adapts the consultant's experience to best match the position. Output is structured and validatable against the CV content schema."

                contentValidator = component "Content Validator" "Validates the LLM's CV content document against the CV content schema. Rejects malformed output. On rejection: emits an error to Observability and notifies the Consultant that generation failed. Does not fall back silently."

                cvRenderer = component "CV Renderer" "Deterministic step. Takes the validated CV content document and applies the HTML template, producing the rendered CV. No LLM involvement. If the position expired during generation (detected by comparing Job Store status at render time), flags the rendered CV with an expiry warning for the Consultant."

                experienceCapture = component "Experience Capture" "Handles in-session free-text experience input from the Consultant during CV generation. Calls the LLM with the free-text input and the experience database schema (experience-db.schema.md) to interpret and map the input to the correct database fields. Writes structured entries to the Profile & Configuration Store. Designed as the seam for future chatbot integration."

                cvNotifier = component "CV Notifier" "Sends the CV-ready notification to the Talent Advisor when rendering is complete. Includes the expiry warning in the notification payload if the position closed during generation. Writes the CV generation event to the Application Tracker."
            }
        }

        # ─────────────────────────────────────────────
        # RELATIONSHIPS — ACTORS TO SYSTEMS (L1)
        # ─────────────────────────────────────────────

        consultant -> opportunityMatchingSystem "Reviews matched positions and submits verdicts via"
        consultant -> cvPreparationSystem "Triggers CV generation and inputs experience via"
        talentAdvisor -> opportunityMatchingSystem "Configures active consultants and job board sources via"
        opportunityMatchingSystem -> talentAdvisor "Notifies when a consultant accepts a position"
        talentAdvisor -> cvPreparationSystem "Receives CV-ready notifications from"
        systemAdministrator -> dataLayer "Monitors system health and reviews held configurations via"
        opportunityMatchingSystem -> jobBoards "Collects raw job postings from"
        cvPreparationSystem -> jobBoards "Reads position source metadata from" "indirect via Job Store"

        # ─────────────────────────────────────────────
        # RELATIONSHIPS — CONTAINERS TO STORES (L2)
        # ─────────────────────────────────────────────

        # Source Collector
        opportunityMatchingSystem.sourceCollector -> dataLayer.taConfigStore "Reads configured job board sources from"
        opportunityMatchingSystem.sourceCollector -> dataLayer.jobStore "Writes new positions to"
        opportunityMatchingSystem.sourceCollector -> dataLayer.observability "Emits collection cycle logs and alerts to"

        # Pre-Filter & Matcher
        opportunityMatchingSystem.preFilterMatcher -> dataLayer.jobStore "Reads active positions from"
        opportunityMatchingSystem.preFilterMatcher -> dataLayer.profileConfigStore "Reads current matching configuration from"
        opportunityMatchingSystem.preFilterMatcher -> dataLayer.observability "Emits scoring errors to"

        # LLM Standardizer
        opportunityMatchingSystem.llmStandardizer -> dataLayer.jobStore "Writes standardized descriptions to"
        opportunityMatchingSystem.llmStandardizer -> dataLayer.observability "Emits partial-failure alerts to"
        opportunityMatchingSystem.llmStandardizer -> llmProvider "Sends batch standardization prompt to"

        # Dashboard
        opportunityMatchingSystem.dashboard -> dataLayer.jobStore "Reads standardized position descriptions from"
        opportunityMatchingSystem.dashboard -> dataLayer.applicationTracker "Writes completed verdict sets to"
        opportunityMatchingSystem.dashboard -> dataLayer.observability "Emits cycle completion events and UI errors to"

        # Learning Module
        opportunityMatchingSystem.learningModule -> dataLayer.applicationTracker "Reads full verdict history from"
        opportunityMatchingSystem.learningModule -> dataLayer.profileConfigStore "Reads current configuration and consultant profile from"
        opportunityMatchingSystem.learningModule -> dataLayer.profileConfigStore "Writes updated matching configuration to"
        opportunityMatchingSystem.learningModule -> dataLayer.observability "Emits schema validation failures and delta-guard holds to"
        opportunityMatchingSystem.learningModule -> llmProvider "Sends configuration update prompt to"

        # CV Importer
        cvPreparationSystem.cvImporter -> dataLayer.profileConfigStore "Writes structured experience entries to"
        cvPreparationSystem.cvImporter -> llmProvider "Sends experience extraction prompt to"
        cvPreparationSystem.cvImporter -> dataLayer.observability "Emits import and extraction errors to"

        # CV Pipeline
        cvPreparationSystem.cvPipeline -> dataLayer.jobStore "Reads position text and checks validity against"
        cvPreparationSystem.cvPipeline -> dataLayer.profileConfigStore "Reads consultant experience from; writes in-session experience entries to"
        cvPreparationSystem.cvPipeline -> dataLayer.applicationTracker "Writes CV generation events to"
        cvPreparationSystem.cvPipeline -> dataLayer.observability "Emits pipeline errors to"
        cvPreparationSystem.cvPipeline -> llmProvider "Sends CV content generation and experience capture prompts to"

        # Actor interactions with containers
        consultant -> opportunityMatchingSystem.dashboard "Reviews positions and submits verdicts via"
        consultant -> cvPreparationSystem.cvImporter "Uploads existing CVs via"
        consultant -> cvPreparationSystem.cvPipeline "Inputs in-session experience and triggers CV generation via"
        talentAdvisor -> dataLayer.taConfigStore "Configures active consultants and job board sources in"
        opportunityMatchingSystem.dashboard -> talentAdvisor "Notifies when consultant accepts a position"
        talentAdvisor -> cvPreparationSystem.cvPipeline "Receives CV-ready notification from"
        systemAdministrator -> dataLayer.observability "Monitors alerts and reviews held configurations via"

        # ─────────────────────────────────────────────
        # RELATIONSHIPS — COMPONENTS (L3)
        # Opportunity Matching Pipeline internals
        # ─────────────────────────────────────────────

        # Source Collector internals
        opportunityMatchingSystem.sourceCollector.scheduler -> opportunityMatchingSystem.sourceCollector.boardConnector "Triggers collection cycle"
        opportunityMatchingSystem.sourceCollector.boardConnector -> dataLayer.taConfigStore "Reads configured sources from"
        opportunityMatchingSystem.sourceCollector.boardConnector -> jobBoards "Fetches raw postings from"
        opportunityMatchingSystem.sourceCollector.boardConnector -> opportunityMatchingSystem.sourceCollector.positionWriter "Passes fetched positions to"
        opportunityMatchingSystem.sourceCollector.positionWriter -> dataLayer.jobStore "Deduplicates and writes new positions to"
        opportunityMatchingSystem.sourceCollector.positionWriter -> dataLayer.observability "Emits collection summary to"
        opportunityMatchingSystem.sourceCollector.scheduler -> dataLayer.observability "Emits cycle failure alert to"

        # Pre-Filter & Matcher internals
        opportunityMatchingSystem.preFilterMatcher.configReader -> dataLayer.profileConfigStore "Reads and validates matching configuration from"
        opportunityMatchingSystem.preFilterMatcher.configReader -> opportunityMatchingSystem.preFilterMatcher.scoringEngine "Passes validated configuration to"
        opportunityMatchingSystem.preFilterMatcher.scoringEngine -> dataLayer.jobStore "Reads active positions from"
        opportunityMatchingSystem.preFilterMatcher.scoringEngine -> opportunityMatchingSystem.preFilterMatcher.batchAssembler "Passes scored position list to"
        opportunityMatchingSystem.preFilterMatcher.explorationSelector -> dataLayer.jobStore "Reads candidate positions for diversity selection from"
        opportunityMatchingSystem.preFilterMatcher.explorationSelector -> opportunityMatchingSystem.preFilterMatcher.batchAssembler "Passes exploration positions to"
        opportunityMatchingSystem.preFilterMatcher.batchAssembler -> opportunityMatchingSystem.llmStandardizer "Passes final batch of 10 to"

        # LLM Standardizer internals
        opportunityMatchingSystem.llmStandardizer.batchPromptBuilder -> opportunityMatchingSystem.llmStandardizer.responseParser "Passes prompt and raw LLM response to"
        opportunityMatchingSystem.llmStandardizer.batchPromptBuilder -> llmProvider "Sends batched standardization prompt to"
        opportunityMatchingSystem.llmStandardizer.responseParser -> dataLayer.jobStore "Writes standardized descriptions to"
        opportunityMatchingSystem.llmStandardizer.responseParser -> dataLayer.observability "Emits partial-failure alert if descriptions are missing"

        # Dashboard internals
        opportunityMatchingSystem.dashboard.positionRenderer -> dataLayer.jobStore "Reads standardized descriptions and match scores from"
        opportunityMatchingSystem.dashboard.positionRenderer -> opportunityMatchingSystem.dashboard.verdictCapture "Renders position alongside verdict form"
        opportunityMatchingSystem.dashboard.verdictCapture -> opportunityMatchingSystem.dashboard.cycleController "Submits completed verdict to"
        opportunityMatchingSystem.dashboard.cycleController -> dataLayer.applicationTracker "Writes completed verdict set to"
        opportunityMatchingSystem.dashboard.cycleController -> opportunityMatchingSystem.learningModule "Triggers Learning Module on cycle completion"
        opportunityMatchingSystem.dashboard.cycleController -> talentAdvisor "Notifies on consultant position acceptance"
        consultant -> opportunityMatchingSystem.dashboard.positionRenderer "Views standardized positions via"
        consultant -> opportunityMatchingSystem.dashboard.verdictCapture "Submits verdicts and reasoning via"

        # Learning Module internals
        opportunityMatchingSystem.learningModule.historyAggregator -> dataLayer.applicationTracker "Reads verdict history from"
        opportunityMatchingSystem.learningModule.historyAggregator -> opportunityMatchingSystem.learningModule.configUpdatePromptBuilder "Passes structured history to"
        opportunityMatchingSystem.learningModule.configUpdatePromptBuilder -> dataLayer.profileConfigStore "Reads current configuration and profile from"
        opportunityMatchingSystem.learningModule.configUpdatePromptBuilder -> llmProvider "Sends configuration update prompt to"
        opportunityMatchingSystem.learningModule.configUpdatePromptBuilder -> opportunityMatchingSystem.learningModule.schemaValidator "Passes LLM response to"
        opportunityMatchingSystem.learningModule.schemaValidator -> opportunityMatchingSystem.learningModule.deltaGuard "Passes validated configuration to"
        opportunityMatchingSystem.learningModule.schemaValidator -> dataLayer.observability "Emits schema rejection errors to"
        opportunityMatchingSystem.learningModule.deltaGuard -> opportunityMatchingSystem.learningModule.configWriter "Passes approved configuration to"
        opportunityMatchingSystem.learningModule.deltaGuard -> dataLayer.observability "Emits delta-guard hold notifications to"
        opportunityMatchingSystem.learningModule.configWriter -> dataLayer.profileConfigStore "Writes updated configuration to"

        # ─────────────────────────────────────────────
        # RELATIONSHIPS — COMPONENTS (L3)
        # CV Preparation Pipeline internals
        # ─────────────────────────────────────────────

        # CV Importer internals
        consultant -> cvPreparationSystem.cvImporter.fileReceiver "Uploads CV file via"
        cvPreparationSystem.cvImporter.fileReceiver -> cvPreparationSystem.cvImporter.cvParser "Passes validated file to"
        cvPreparationSystem.cvImporter.cvParser -> cvPreparationSystem.cvImporter.experienceExtractor "Passes raw extracted text to"
        cvPreparationSystem.cvImporter.experienceExtractor -> llmProvider "Sends experience extraction prompt to"
        cvPreparationSystem.cvImporter.experienceExtractor -> cvPreparationSystem.cvImporter.cvImporterExperienceWriter "Passes LLM-structured experience entries to"
        cvPreparationSystem.cvImporter.cvImporterExperienceWriter -> dataLayer.profileConfigStore "Writes validated experience entries to"
        cvPreparationSystem.cvImporter.cvImporterExperienceWriter -> dataLayer.observability "Emits schema validation errors to"

        # CV Pipeline internals
        cvPreparationSystem.cvPipeline.handoffReceiver -> dataLayer.jobStore "Checks position validity against"
        cvPreparationSystem.cvPipeline.handoffReceiver -> consultant "Emits expiry notification to"
        cvPreparationSystem.cvPipeline.handoffReceiver -> cvPreparationSystem.cvPipeline.contextAssembler "Triggers on valid position"
        cvPreparationSystem.cvPipeline.contextAssembler -> dataLayer.jobStore "Reads raw position text from"
        cvPreparationSystem.cvPipeline.contextAssembler -> dataLayer.profileConfigStore "Reads consultant experience database from"
        cvPreparationSystem.cvPipeline.contextAssembler -> cvPreparationSystem.cvPipeline.cvContentGenerator "Passes assembled context to"
        cvPreparationSystem.cvPipeline.cvContentGenerator -> llmProvider "Sends CV content generation prompt to"
        cvPreparationSystem.cvPipeline.cvContentGenerator -> cvPreparationSystem.cvPipeline.contentValidator "Passes LLM content document to"
        cvPreparationSystem.cvPipeline.contentValidator -> cvPreparationSystem.cvPipeline.cvRenderer "Passes validated content document to"
        cvPreparationSystem.cvPipeline.contentValidator -> dataLayer.observability "Emits content validation errors to"
        cvPreparationSystem.cvPipeline.contentValidator -> consultant "Notifies on generation failure"
        cvPreparationSystem.cvPipeline.cvRenderer -> cvPreparationSystem.cvPipeline.cvNotifier "Passes rendered CV to"
        cvPreparationSystem.cvPipeline.cvRenderer -> dataLayer.jobStore "Checks final position status for expiry flag"
        consultant -> cvPreparationSystem.cvPipeline.experienceCapture "Inputs free-text experience during CV session via"
        cvPreparationSystem.cvPipeline.experienceCapture -> llmProvider "Sends experience interpretation prompt to"
        cvPreparationSystem.cvPipeline.experienceCapture -> dataLayer.profileConfigStore "Writes structured in-session experience entries to"
        cvPreparationSystem.cvPipeline.experienceCapture -> dataLayer.observability "Emits capture errors to"
        cvPreparationSystem.cvPipeline.cvNotifier -> talentAdvisor "Sends CV-ready notification to"
        cvPreparationSystem.cvPipeline.cvNotifier -> dataLayer.applicationTracker "Writes CV generation event to"
        cvPreparationSystem.cvPipeline.cvNotifier -> dataLayer.observability "Emits notification errors to"
    }

    # ─────────────────────────────────────────────
    # VIEWS
    # ─────────────────────────────────────────────

    views {

        # L1 — System Context
        systemContext opportunityMatchingSystem "L1_SystemContext" "System Context: all actors and systems in scope for MVP 1.0." {
            include *
            exclude dataLayer
            autoLayout
        }

        # L2 — Container View: Opportunity Matching Pipeline
        container opportunityMatchingSystem "L2_OpportunityMatchingContainers" "Container View: all containers in the Opportunity Matching Pipeline and the shared Data Layer." {
            include *
            autoLayout
        }

        # L2 — Container View: CV Preparation Pipeline
        container cvPreparationSystem "L2_CVPreparationContainers" "Container View: all containers in the CV Preparation Pipeline and the shared Data Layer." {
            include *
            autoLayout
        }

        # L3 — Component View: Source Collector
        component opportunityMatchingSystem.sourceCollector "L3_SourceCollector" "Component View: internal structure of the Source Collector container." {
            include *
            include jobBoards
            include dataLayer.taConfigStore
            include dataLayer.jobStore
            include dataLayer.observability
            autoLayout
        }

        # L3 — Component View: Pre-Filter & Matcher
        component opportunityMatchingSystem.preFilterMatcher "L3_PreFilterMatcher" "Component View: internal structure of the Pre-Filter and Matcher container." {
            include *
            autoLayout
        }

        # L3 — Component View: LLM Standardizer
        component opportunityMatchingSystem.llmStandardizer "L3_LLMStandardizer" "Component View: internal structure of the LLM Standardizer container." {
            include *
            autoLayout
        }

        # L3 — Component View: Dashboard
        component opportunityMatchingSystem.dashboard "L3_Dashboard" "Component View: internal structure of the Dashboard container." {
            include *
            autoLayout
        }

        # L3 — Component View: Learning Module
        component opportunityMatchingSystem.learningModule "L3_LearningModule" "Component View: internal structure of the Learning Module container." {
            include *
            autoLayout
        }

        # L3 — Component View: CV Importer
        component cvPreparationSystem.cvImporter "L3_CVImporter" "Component View: internal structure of the CV Importer container. Shows the file upload, parsing, LLM-based experience extraction, and write path to the Profile & Configuration Store." {
            include *
            include llmProvider
            include dataLayer.profileConfigStore
            include dataLayer.observability
            autoLayout
        }

        # L3 — Component View: CV Pipeline
        component cvPreparationSystem.cvPipeline "L3_CVPipeline" "Component View: internal structure of the CV Pipeline container. Shows the full generation flow from handoff validation through content generation, rendering, in-session experience capture, and Talent Advisor notification." {
            include *
            include consultant
            include talentAdvisor
            include llmProvider
            include dataLayer.jobStore
            include dataLayer.profileConfigStore
            include dataLayer.applicationTracker
            include dataLayer.observability
            autoLayout
        }

        # ─────────────────────────────────────────────
        # STYLES
        # ─────────────────────────────────────────────

        styles {

            element "Person" {
                shape Person
                background #1168BD
                color #FFFFFF
                fontSize 22
            }

            element "External" {
                background #999999
                color #FFFFFF
            }

            element "Internal" {
                background #1168BD
                color #FFFFFF
            }

            element "Database" {
                shape Cylinder
                background #438DD5
                color #FFFFFF
            }

            element "Observability" {
                shape Hexagon
                background #85BBF0
                color #000000
            }

            element "WebBrowser" {
                shape WebBrowser
                background #438DD5
                color #FFFFFF
            }

            element "Component" {
                background #85BBF0
                color #000000
            }

            element "Software System" {
                background #1168BD
                color #FFFFFF
            }
        }
    }
}
