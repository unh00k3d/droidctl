plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.plugin.compose")
}

android {
    namespace = "dev.droidctl.testapp"
    compileSdk = 35

    defaultConfig {
        applicationId = "dev.droidctl.testapp"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"
    }

    // The agent's committed key: builds are reproducible across machines, so
    // `adb install -r` always upgrades instead of failing on a signature clash.
    signingConfigs {
        create("droidctl") {
            storeFile = file("../agent/droidctl-debug.keystore")
            storePassword = "android"
            keyAlias = "droidctl"
            keyPassword = "android"
        }
    }

    buildTypes {
        // the debug build is the one we install: it must stay debuggable so
        // tests can `run-as` into the app's uid (peer_uid_probe)
        getByName("debug") {
            signingConfig = signingConfigs.getByName("droidctl")
        }
    }

    buildFeatures {
        compose = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    testOptions {
        unitTests.isReturnDefaultValues = true
    }
}

dependencies {
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.recyclerview:recyclerview:1.4.0")
    implementation("androidx.viewpager2:viewpager2:1.1.0")
    implementation("androidx.swiperefreshlayout:swiperefreshlayout:1.1.0")

    implementation(platform("androidx.compose:compose-bom:2025.04.01"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.foundation:foundation")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.activity:activity-compose:1.10.1")

    testImplementation("junit:junit:4.13.2")
}

// scenarios.json lives next to this file; the manifest test compares or rewrites it
tasks.withType<Test>().configureEach {
    systemProperty("droidctl.scenariosJson", file("scenarios.json").absolutePath)
    inputs.file(file("scenarios.json")).optional()
    project.findProperty("droidctl.updateScenarios")?.let { systemProperty("droidctl.updateScenarios", it.toString()) }
}
