plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

android {
    namespace = "com.novelclaw.app"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.novelclaw.app"
        minSdk = 26
        targetSdk = 34
        versionCode = 1
        versionName = "0.1.0-android"
        ndk {
            abiFilters += listOf("arm64-v8a", "x86_64")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    packaging {
        resources.excludes += listOf("META-INF/*.kotlin_module")
    }
}

// Stage the Python sources from ../../apps into the layout used on device:
//   android_server.py, inprocess_worker.py, novelclaw/, multiagent/,
//   local_web_portal/ (= apps/auth-portal/local_web_portal)
val appsDir = rootProject.projectDir.resolve("../apps")
val stagedPython = layout.buildDirectory.dir("python-src")
val stagePython by tasks.registering(Sync::class) {
    into(stagedPython)
    from(appsDir) {
        include("android_server.py", "inprocess_worker.py", "novelclaw/**", "multiagent/**")
    }
    from(appsDir.resolve("auth-portal")) {
        include("local_web_portal/**")
    }
    exclude("**/__pycache__/**", "**/*.pyc", "**/.env", "**/*.ps1", "**/*.bat", "**/data/*.db",
        "**/runs/**", "**/vector_db/**", "**/Dockerfile", "**/docker-compose.yml", "**/*.md")
}
tasks.matching { it.name.startsWith("merge") && it.name.endsWith("PythonSources") }.configureEach {
    dependsOn(stagePython)
}
tasks.matching { it.name.contains("Python") && it.name != "stagePython" }.configureEach {
    dependsOn(stagePython)
}

chaquopy {
    defaultConfig {
        version = "3.11"
        (project.findProperty("chaquopyBuildPython") as String?)?.let { buildPython(it) }
        pip {
            // pydantic v1's pure-Python wheel; pydantic-core has no Android build.
            options("--prefer-binary")
            install("-r", rootProject.projectDir.resolve("../requirements-android.txt").path)
        }
        // Jinja templates and static files are read from the filesystem.
        extractPackages("novelclaw", "multiagent", "local_web_portal")
    }
    sourceSets {
        getByName("main") {
            setSrcDirs(listOf(stagedPython.get().asFile.path))
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.webkit:webkit:1.11.0")
}
