plugins {
    id("com.android.application") version "9.4.1" apply false
    // must match the Kotlin Gradle plugin that AGP 9.4.1 bundles (2.2.10)
    id("org.jetbrains.kotlin.plugin.compose") version "2.2.10" apply false
}
