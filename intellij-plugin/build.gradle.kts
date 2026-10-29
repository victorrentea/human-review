import org.jetbrains.intellij.platform.gradle.TestFrameworkType
import org.jetbrains.intellij.platform.gradle.tasks.VerifyPluginTask.FailureLevel
import org.jetbrains.kotlin.gradle.dsl.JvmTarget

// Trimmed from ~/workspace/live-coding: same Gradle, Kotlin and IntelliJ Platform Gradle
// Plugin versions (they are known to build together on this machine), without the changelog,
// signing and bundled-plugin dependencies this plugin does not need. Marketplace publishing is
// at the bottom (`publishing`), driven by publish.sh.

fun properties(key: String) = providers.gradleProperty(key)

plugins {
    id("java")
    id("org.jetbrains.kotlin.jvm") version "2.1.0"
    id("org.jetbrains.intellij.platform") version "2.16.0"
}

group = properties("pluginGroup").get()
version = properties("pluginVersion").get()

repositories {
    mavenCentral()
    intellijPlatform {
        defaultRepositories()
    }
}

dependencies {
    intellijPlatform {
        intellijIdeaCommunity(properties("platformVersion").get())
        testFramework(TestFrameworkType.Platform)
    }
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.opentest4j:opentest4j:1.3.0")
}

intellijPlatform {
    pluginConfiguration {
        id = "ro.victorrentea.human-review"
        name = "Human Review"
        version = properties("pluginVersion")
        vendor {
            name = "Victor Rentea"
            url = "https://github.com/victorrentea/human-review"
        }
        description = """
            The IntelliJ end of a <a href="https://github.com/victorrentea/human-review">/human-review</a>
            page — a code review written for a human to read, full of <code>File.java:49-51</code>
            references.
            <ul>
              <li><b>References open in the right window</b>: the project window that has the
                  reviewed checkout, as a highlighted range with the rest of the file muted.</li>
              <li><b>The reviewed version, or a plain no</b>: a reference opens only where the file
                  is exactly as it was reviewed, and says so when no window has it.</li>
              <li><b>Fixes as diffs</b>: the file at the review's base on the left, your working
                  tree on the right.</li>
              <li><b>The page beside the code</b>, in a Human Review tool window (JCEF).</li>
              <li><b>PR comments</b>: a card posted to the pull request lands on its lines and
                  offers the thread on GitHub.</li>
              <li>A <b>human-review</b> status-bar item: is this checkout's review served? Click
                  to serve it and open the page.</li>
              <li><b>GitHub pull requests</b>: the bundled GitHub plugin's Pull Requests tool
                  window follows the editor in a PR's changes tree, its status block folds to one
                  line, and a review page opens a PR there.</li>
            </ul>
            Installs and updates without restarting the IDE.
        """.trimIndent()
        changeNotes = """
            <h3>0.3.6</h3>
            <ul>
              <li><b>Follow the editor</b> works right after a live update, without waiting for a
                  restart.</li>
            </ul>
            <h3>0.3.5</h3>
            <ul>
              <li><b>The Pull Requests tool window follows the editor</b>: switching the editor to
                  a file of the open pull request selects it in the PR's changes tree and scrolls
                  there. On by default; right-click the tree → <i>Follow the Editor</i>, or
                  Settings → Tools → Human Review.</li>
              <li><b>A foldable status block</b>: double-click the checks and reviewers block under
                  the changes tree to fold it to its first line and give the tree the room; again
                  to unfold. Remembered; also in the tree's right-click menu.</li>
              <li><b>Open a PR in IntelliJ</b>: a review page's <i>in IntelliJ</i> beside a review
                  posted to the PR opens it in the Pull Requests tool window, its tab selected.</li>
              <li><b>Fixed</b>: a live update could end in "not unload-safe" and a restart — an
                  editor or a diff opened from a review link kept the previous build's classes
                  alive. Editors, diffs and tool windows are now opened off the plugin's own
                  stack.</li>
            </ul>
            <h3>0.3.1</h3>
            <ul>
              <li>No internal or deprecated IntelliJ API through 2026.3: the plugin's version, a
                  project's content roots and opening a file at a line now go through public,
                  non-deprecated calls that also exist in 2024.3.</li>
            </ul>
            <h3>0.3.0</h3>
            <ul>
              <li><b>The review page inside IntelliJ</b>: a <i>Human Review</i> tool window with an
                  embedded browser (JCEF). <code>serve-review.py</code> started from IntelliJ's
                  terminal shows the page there, and the status-bar item opens it there too
                  (Settings → Tools → Human Review to use the OS browser instead).</li>
              <li><b>PR comments</b>: a card posted to the pull request lands on its lines and
                  offers <i>Open on GitHub</i> for its thread — IntelliJ has no API to expand a
                  review thread in the editor.</li>
              <li><b>Fixed</b>: a highlighted range sometimes vanished a second after a file opened
                  cold — the open's own delayed caret move was taken for the reader's.</li>
              <li><b>Updates without a restart</b>: the plugin unloads cleanly (its listener port,
                  registry entry, highlights and notifications go with it).</li>
            </ul>
            <h3>0.2.0</h3>
            <ul>
              <li><b>Before/after diffs</b>: a fix on the review page opens as a diff — the file at
                  the base commit on the left, read-only; the working tree on the right, editable —
                  scrolled to the line the fix is about.</li>
              <li><b>The reviewed version, or a plain refusal</b>: a reference opens only in a
                  window whose checkout has the file exactly as it was reviewed (here, or in
                  another IntelliJ or VS Code window); otherwise it says so, with a prompt to copy
                  for a coding agent. A file edited since is marked on the line you land on.</li>
              <li><b>Pages read off disk</b>: <code>jetbrains://idea/human-review/…</code> links
                  (open, diff, review-open) reach the plugin with no review server running
                  (needs JetBrains Toolbox, which owns the <code>jetbrains://</code> scheme).</li>
              <li>A plugin icon.</li>
            </ul>
            <h3>0.1.0</h3>
            <ul>
              <li>References open in the project window that owns the file, as a highlighted range.</li>
              <li>A <b>human-review</b> status-bar item: is this checkout's review served?</li>
            </ul>
        """.trimIndent()
        ideaVersion {
            sinceBuild = properties("pluginSinceBuild")
            untilBuild = provider { null }
        }
    }

    pluginVerification {
        failureLevel = listOf(
            FailureLevel.COMPATIBILITY_PROBLEMS,
            FailureLevel.INVALID_PLUGIN,
            FailureLevel.MISSING_DEPENDENCIES,
            // Installed and updated without a restart (Lifecycle.kt): a non-dynamic extension
            // point or a component would quietly make every update a restart again.
            FailureLevel.NOT_DYNAMIC,
            // The Marketplace lists both on the plugin page; 0.3.0 shipped with three of them.
            FailureLevel.INTERNAL_API_USAGES,
            FailureLevel.DEPRECATED_API_USAGES,
        )
        ides {
            // The oldest IDE it claims (sinceBuild 243), the one it is built against, and the
            // ones the Marketplace verifier runs: from 2025.3 IntelliJ IDEA is one product (IU).
            create(properties("platformType").get(), "2024.3")
            create(properties("platformType").get(), properties("platformVersion").get())
            create("IU", "2026.1.5")
            create("IU", "2026.2.3")
            create("IU", "263.7031.23")
        }
    }

    // `./publish.sh`: every change to the plugin goes out as a new Marketplace version. The
    // token is Victor's, read from the environment and never from a file in the repository.
    publishing {
        token = providers.environmentVariable("JETBRAINS_PUBLISH_TOKEN")
    }
}

kotlin {
    jvmToolchain(21)
    compilerOptions {
        jvmTarget = JvmTarget.JVM_21
    }
}

java {
    sourceCompatibility = JavaVersion.VERSION_21
    targetCompatibility = JavaVersion.VERSION_21
}

tasks {
    wrapper {
        gradleVersion = properties("gradleVersion").get()
    }

    test {
        // Never the real ~/.human-review/ide/: a bridge a test starts must not be routed to, and
        // a hand-over a test makes must not reach an editor someone is working in.
        systemProperty("human-review.registry.dir", layout.buildDirectory.dir("test-registry").get().asFile.path)
        systemProperty("java.awt.headless", "true")
    }

    runIde {
        // A sandbox IDE asks "Trust this project?" in a modal, which blocks the EDT — and every
        // bridge route that runs on the EDT with it, so a curl against a fresh sandbox hangs.
        jvmArgs("-Didea.trust.all.projects=true")
    }
}
