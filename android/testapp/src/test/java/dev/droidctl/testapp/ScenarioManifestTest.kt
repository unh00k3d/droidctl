package dev.droidctl.testapp

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * scenarios.json must equal what the registry generates. To refresh it:
 *   ./gradlew :testapp:testDebugUnitTest -Pdroidctl.updateScenarios=true
 */
class ScenarioManifestTest {
    private val file = File(System.getProperty("droidctl.scenariosJson") ?: "scenarios.json")

    @Test fun scenariosJsonMatchesTheRegistry() {
        val want = specsJson()
        if (System.getProperty("droidctl.updateScenarios") == "true") file.writeText(want)
        assertTrue("missing ${file.absolutePath}; run with -Pdroidctl.updateScenarios=true", file.exists())
        assertEquals("scenarios.json is stale; regenerate it (see this test's doc)", want, file.readText())
    }

    @Test fun namesAreUniqueAndGroupsKnown() {
        assertEquals(SPECS.size, SPECS.map { it.name }.toSet().size)
        assertTrue(SPECS.all { it.group in setOf(0, 1, 2, 3, 4, 5, 6, 8) && it.toolkit in setOf("views", "compose") })
    }
}
