# Android Integration — PANDA VOICE AI

Base URL assigned by Railway:

https://panda-voice-ai-production.up.railway.app/

Fish Audio credentials stay on the server. Your Android client calls the PANDA backend, never Fish Audio directly.

## Authentication

Every API request must include:

X-API-Key: YOUR_PANDA_API_KEY

The server-side key is stored in Railway as PANDA_API_KEY. Copy its value from the service Variables page. Do not commit it to source control.

Security: a key embedded in an APK can be extracted. For a public app, have your own backend authenticate users and call PANDA VOICE AI server-to-server. For a private test app, protect the key and rotate it if the APK is distributed.

## 1. List voice models

GET https://panda-voice-ai-production.up.railway.app/api/v1/voices?gender=female&style=story&page=1&page_size=20

Query parameters:
- gender: all, female, or male
- style: natural, story, podcast, voiceover, audiobook, news, documentary, meditation, energetic, whisper, empathetic, or commercial

Response example:

```json
{
  "items": [
    {
      "id": "FISH_AUDIO_VOICE_MODEL_ID",
      "name": "Voice name",
      "gender": "female",
      "tags": ["female", "narration"],
      "languages": ["English"],
      "style_match": 1
    }
  ],
  "count": 1,
  "page": 1,
  "page_size": 20,
  "has_more": false,
  "gender": "female",
  "style": "story"
}
```

## 2. List speaking styles and supported engines

- GET /api/v1/styles
- GET /api/v1/engines

Both require X-API-Key. The engine list identifies S2.1 Pro Free as free; the other engines may require Fish Audio API credit.

## 3. Preview a voice or style

POST https://panda-voice-ai-production.up.railway.app/api/v1/preview

Use the same JSON body as the TTS endpoint. The response is audio/mpeg for preview playback.

## 4. Generate MP3

POST https://panda-voice-ai-production.up.railway.app/api/v1/tts

Headers:
- X-API-Key: YOUR_PANDA_API_KEY
- Content-Type: application/json

Request:

```json
{
  "text": "Hello from my Android application.",
  "voice_id": "FISH_AUDIO_VOICE_MODEL_ID",
  "style": "natural",
  "speed": 1.0,
  "volume": 0.0
}
```

Success response: raw MP3 bytes with content type audio/mpeg. Save the response body to an .mp3 file and play it with Android Media3/ExoPlayer or MediaPlayer.

Limits:
- speed: 0.5 to 2.0
- volume: -20.0 to 20.0 dB
- text: 1 to the configured maximum character count
- Response audio is not cached by the API.

## 5. Kotlin + Retrofit

Gradle dependencies (use current stable versions in your project):

```kotlin
implementation("com.squareup.retrofit2:retrofit:<version>")
implementation("com.squareup.retrofit2:converter-gson:<version>")
implementation("com.squareup.okhttp3:okhttp:<version>")
```

Add internet permission:

```xml
<uses-permission android:name="android.permission.INTERNET" />
```

Data models:

```kotlin
data class VoiceItem(
    val id: String,
    val name: String,
    val gender: String,
    val tags: List<String> = emptyList(),
    val languages: List<String> = emptyList(),
    val style_match: Int = 0
)

data class VoiceResponse(
    val items: List<VoiceItem>,
    val count: Int,
    val gender: String,
    val style: String
)

data class TtsRequest(
    val text: String,
    val voice_id: String,
    val style: String = "natural",
    val speed: Double = 1.0,
    val volume: Double = 0.0
)
```

Retrofit interface:

```kotlin
import okhttp3.ResponseBody
import retrofit2.Response
import retrofit2.http.Body
import retrofit2.http.GET
import retrofit2.http.Header
import retrofit2.http.POST
import retrofit2.http.Query
import retrofit2.http.Streaming

interface PandaVoiceApi {
    @GET("api/v1/voices")
    suspend fun getVoices(
        @Header("X-API-Key") apiKey: String,
        @Query("gender") gender: String = "all",
        @Query("style") style: String = "natural"
    ): VoiceResponse

    @Streaming
    @POST("api/v1/preview")
    suspend fun preview(
        @Header("X-API-Key") apiKey: String,
        @Body request: TtsRequest
    ): ResponseBody

    @Streaming
    @POST("api/v1/tts")
    suspend fun generateSpeech(
        @Header("X-API-Key") apiKey: String,
        @Body request: TtsRequest
    ): Response<ResponseBody>
}
```

Create the Retrofit client:

```kotlin
val retrofit = Retrofit.Builder()
    .baseUrl("https://panda-voice-ai-production.up.railway.app/")
    .addConverterFactory(GsonConverterFactory.create())
    .build()

val pandaApi = retrofit.create(PandaVoiceApi::class.java)
```

Call the endpoint and save the MP3:

```kotlin
suspend fun createSpeech(
    api: PandaVoiceApi,
    apiKey: String,
    outputFile: File,
    text: String,
    voiceId: String
) {
    val response = api.generateSpeech(
        apiKey,
        TtsRequest(
            text = text,
            voice_id = voiceId,
            style = "story",
            speed = 1.0
        )
    )

    if (!response.isSuccessful) {
        val errorText = response.errorBody()?.string().orEmpty()
        throw IOException("PANDA API error ${response.code()}: $errorText")
    }

    val body = response.body() ?: throw IOException("Empty audio response")
    body.byteStream().use { input ->
        outputFile.outputStream().use { output -> input.copyTo(output) }
    }
}
```

Run network calls on Dispatchers.IO, not the Android main thread. Use Android's app-private files directory for the generated audio file.

## Health check

GET https://panda-voice-ai-production.up.railway.app/health

Expected response:

```json
{"status":"ok","service":"PANDA VOICE AI"}
```
