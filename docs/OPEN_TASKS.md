# Open tasks

These need you. The app cannot do them.

1. **Point Image Library at the Gemini stills.** The library currently holds
   53 images, all of them screenshots and photos from
   `C:\Users\Gulfam\Pictures`. There is not one Gemini-generated image in
   it, so the prototype was cut from screenshots. Add the real folder.
2. **Free space on C:.** About 350 MB is left of 142 GB. Nothing that needs
   to install to the system drive will work until that is cleared. Tests and
   media already go to D:.
3. **Gemini API key**, if you want scripts written for you. Paste an AI
   Studio key into Settings as `AVM_GEMINI_API_KEY`. Without it, Script
   accepts a pasted plan instead, which is how the prototype was made.
4. **Kaggle credentials**, for borrowed GPU time. Settings takes
   `AVM_KAGGLE_USERNAME` and `AVM_KAGGLE_KEY` from your `kaggle.json`.
   Until then no image-to-video or lip-sync has ever run here.
5. **Hugging Face token and Space names**, only if you want the fallback.
   `AVM_HF_TOKEN`, `AVM_HF_SPACE_I2V`, `AVM_HF_SPACE_LIPSYNC`.
6. **Provider signups**, only if you decide to use a paid API. Leave keys
   blank until then. Paid work stays off until you enable it, set a cap and
   authorise the account. Do not open extra accounts to multiply a free
   trial.

FFmpeg 9.0.2 is installed and working. Piper and the `en_US-joe-medium`
voice are installed on D:. Nothing else blocks local use.
