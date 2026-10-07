#!/usr/bin/env bash
set -euo pipefail
# Local signing only: export IME_KS_PASS (any passphrase) first; the key stays in private/ (gitignored).
: "${IME_KS_PASS:?set IME_KS_PASS}"
cd "$(dirname "$0")"
SDK=${ANDROID_SDK_ROOT:-$HOME/Android/Sdk}
BT="$SDK/build-tools/36.0.0"
JAR="$SDK/platforms/android-36/android.jar"
mkdir -p build/classes build/dex private
"$BT/aapt2" compile --dir app/res -o build/resources.zip
"$BT/aapt2" link -I "$JAR" --manifest app/AndroidManifest.xml -o build/resources.apk build/resources.zip
javac -source 8 -target 8 -bootclasspath "$JAR" -d build/classes app/src/org/fedora/sessionime/InertIme.java
"$BT/d8" --lib "$JAR" --min-api 36 --output build/dex build/classes/org/fedora/sessionime/*.class
cp build/resources.apk build/unsigned.apk
(cd build/dex && zip -q ../unsigned.apk classes.dex)
"$BT/zipalign" -f 4 build/unsigned.apk build/aligned.apk
if [[ ! -f private/session-ime.jks ]]; then
  keytool -genkeypair -keystore private/session-ime.jks -storepass "$IME_KS_PASS" -keypass "$IME_KS_PASS" -alias session-ime -keyalg RSA -keysize 3072 -validity 3650 -dname 'CN=Local Fedora Session IME' >/dev/null
  chmod 600 private/session-ime.jks
fi
"$BT/apksigner" sign --ks private/session-ime.jks --ks-pass pass:"$IME_KS_PASS" --out build/fedora-session-inert-ime.apk build/aligned.apk
"$BT/apksigner" verify --verbose build/fedora-session-inert-ime.apk
sha256sum build/fedora-session-inert-ime.apk
