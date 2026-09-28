plugins {
    id("com.android.application")
}

android {
    namespace = "dev.droidctl.agent"
    compileSdk = 35

    defaultConfig {
        applicationId = "dev.droidctl.agent"
        minSdk = 26
        targetSdk = 35
        versionCode = 10
        versionName = "0.4.2"
    }

    // One committed key for every build type, so `adb install -r` always upgrades.
    signingConfigs {
        create("droidctl") {
            storeFile = file("droidctl-debug.keystore")
            storePassword = "android"
            keyAlias = "droidctl"
            keyPassword = "android"
        }
    }

    buildTypes {
        getByName("debug") {
            signingConfig = signingConfigs.getByName("droidctl")
        }
        getByName("release") {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            signingConfig = signingConfigs.getByName("droidctl")
        }
    }

    packaging {
        // Kotlin reflection metadata; the agent never uses reflection
        resources.excludes += "kotlin/**"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}
