"""The shots for 1,094 Unplayed Voicemails.

Loaded by scripts/unplayed_voicemails.py, which provides S. The people in
the photos are stock models. The story is invented.
"""

SCENES = [
    # Cold open: her face, then his, then the two of them. No explanation first.
    S("s01", "woman close up face portrait eyes", beat="hook", mood="reflective", ambience="room",
      characters=["Lena"],
      dialogue=[("Lena",
                 "Ethan. Don't hang up on a message. I know that's a strange thing to ask, coming from me. "
                 "I'm going to say this straight, in case your thumb gets there first. I love you. I'm sick. "
                 "And I am asking you, just this once, to stay long enough to hear the rest.")]),
    S("s02", "sad young man close portrait", mood="reflective", ambience="night",
      characters=["Ethan"],
      narration="That's Lena. I have deleted her voice one thousand and ninety-four times, usually before she "
                "finished saying my name. Tonight it arrived at three in the morning, and my thumb stopped over "
                "the screen. I didn't play it. I didn't delete it. I sat in the dark with her name lighting up "
                "my hand, and I could hear my own pulse."),
    S("s03", "young couple smiling close portrait", mood="reflective", ambience="room",
      characters=["Ethan", "Lena"],
      narration="If you want to understand a man who could do that, don't start with the phone. Start with her. "
                "Lena Ward. Five years. She laughed with her whole face, and she looked at me like I was already "
                "the person I kept meaning to become. I let her believe it. That was the kindest thing I ever did, "
                "and the cruelest."),

    S("s04", "couple sharing umbrella rain close", mood="reflective", ambience="rain",
      lower="Five years earlier", characters=["Lena", "Ethan"],
      dialogue=[("Lena", "You're soaked through."),
                ("Ethan", "The bus is late. It does this."),
                ("Lena", "Then get under here. Fair warning. This umbrella has one working spoke and a personality problem."),
                ("Ethan", "I'm Ethan."),
                ("Lena", "I know. You apologise to the driver when you're late. I've stood behind you for a month. You never turn around.")]),
    S("s05", "smiling young couple portrait", mood="reflective", ambience="room",
      characters=["Ethan", "Lena"],
      narration="I kept that umbrella for three weeks. She never once asked for it. When I finally brought it back, "
                "she looked at me the way you look at someone who has passed a test they didn't know they were taking. "
                "I think I fell in love with her in that doorway, dripping on her mat, too proud to say the sentence "
                "that was already true."),
    S("s06", "couple laughing at kitchen table", mood="reflective", ambience="room",
      characters=["Ethan", "Lena"],
      narration="We lived above a laundromat. Warm cotton, somebody else's dinner, the windows fogged, the floor "
                "humming. I was never happier than at that small table, and I never said so. I thought she could "
                "hear it in the way I stayed. She couldn't. Nobody hears a thing you refuse to say."),

    S("s07", "woman reading book close portrait", mood="reflective", ambience="room",
      characters=["Lena"],
      narration="A school beside a mountain lake asked her to teach through the long winter. She read the letter at "
                "the sink until the water ran cold over her wrists. I watched from the doorway. I already knew I "
                "was going to be the reason she stayed, and I didn't walk in."),
    S("s08", "young couple having an argument", mood="reflective", ambience="room",
      characters=["Lena", "Ethan"],
      dialogue=[("Lena", "Come with me. A lake, a job, a sky with nothing in the way."),
                ("Ethan", "Everything I do is here."),
                ("Lena", "Everything you do is make other people's voices sound braver than they are. Come and hear mine."),
                ("Ethan", "That isn't fair."),
                ("Lena", "No. It isn't. Come anyway.")]),
    S("s09", "sad couple facing each other argument", mood="reflective", ambience="room",
      characters=["Ethan", "Lena"],
      dialogue=[("Ethan", "Then go. You always wanted the quiet. Go and be quiet."),
                ("Lena", "Okay, Ethan."),
                ("Lena", "I'll call you when I get there. Please pick up.")]),
    S("s10", "woman on the phone at night portrait", mood="reflective", ambience="night",
      characters=["Ethan"],
      narration="She called the first night, from a motel with thin walls. Tired, and too bright, which was the voice "
                "she used when she was trying not to need anyone. I heard my name start, and I pressed delete before "
                "it could land. I told myself it was kindness. It was fear. They feel almost the same, if you don't "
                "listen all the way through."),

    S("s11", "man holding smartphone in a dark room", mood="reflective", ambience="night",
      characters=["Ethan"],
      narration="After that, delete was the whole relationship. A loon on the water. A boy in her class. Weather. "
                "Her name lit the glass, my thumb moved, and the message was gone before the room could change. I "
                "got fast at it. I was proud of the speed, the way you get proud of a habit that is quietly ending you."),
    S("s12", "lonely man lying in bed", mood="reflective", ambience="night",
      characters=["Ethan"],
      narration="I knew that if I heard her to the end, I would get in the car. So I decided I was right, and being "
                "right was a small warm room. It worked, until the room got smaller than I was. I stayed, because "
                "leaving meant hearing her."),
    S("s13", "sad little boy portrait by a window", mood="reflective", ambience="room",
      characters=["Ethan"],
      narration="The rule is older than Lena. I was nine. My father left a message and said he would be home for my "
                "birthday. I played that tape every night until the sound went thin, and he never came. A voice you "
                "keep can hurt worse than silence. I learned it with my ear against a machine, and I have been "
                "protecting myself with it ever since."),
    S("s14", "adult man and woman talking at table", mood="reflective", ambience="room",
      characters=["Maya", "Ethan"],
      dialogue=[("Maya", "How many is it now?"),
                ("Ethan", "You already know."),
                ("Maya", "Say the number. Out loud. In this kitchen."),
                ("Ethan", "One thousand and ninety-four."),
                ("Maya", "She calls me on Sundays. She still says your name like it costs her something to get it out."),
                ("Ethan", "Then tell her to stop spending it.")]),
    S("s15", "man and woman sitting on couch talking", mood="reflective", ambience="room",
      characters=["Maya", "Ethan"],
      dialogue=[("Maya", "Listen to one. Not the pile. One. If you can hear her and still delete the next, I will never ask again."),
                ("Ethan", "That's a trap."),
                ("Maya", "It's a door. You just don't like the room it opens into."),
                ("Ethan", "And if I can't delete the next one?"),
                ("Maya", "Then you get in the car. Like a person. Not like a verdict.")]),
    S("s16", "young man wearing headphones portrait", mood="reflective", ambience="night",
      lower="The first message", characters=["Ethan"],
      narration="The phone was nearly full. Three years of her, almost all of it erased, and one message still "
                "sitting at the bottom. The oldest. The first. I told myself I was only checking the date. My thumb "
                "knew the other button better. I pressed play. The room went quiet in the way a room does when it "
                "has been waiting for you to catch up."),

    S("s17", "gentle smiling young woman portrait", mood="reflective", ambience="wind",
      characters=["Lena"],
      dialogue=[("Lena",
                 "Hi. It's me. Obviously. I'm at the motel. There's a loon out on the lake and it sounds like "
                 "somebody laughing in the next room. I wanted you to hear it before I slept. I love you. Okay. Bye.")]),
    S("s18", "sad young man portrait close", mood="reflective", ambience="night",
      characters=["Ethan"],
      narration="Eleven seconds. I played it four times, and then a fifth, because the loon laughed at the end and "
                "I wanted to prove I could hear it without coming apart. I couldn't. I put my hand over my mouth, "
                "the way you do when the walls are thin, and I pressed the next one before I could talk myself "
                "back into being a coward."),
    S("s19", "smiling woman teacher in a classroom", mood="reflective", ambience="room",
      characters=["Lena"],
      dialogue=[("Lena",
                 "Darnell drew a cell as a pizza today, then apologised for the pepperoni. I told him the "
                 "mitochondria were the pepperoni. He thinks he's a genius. I'm not correcting him. You would have "
                 "corrected him, Ethan, and then felt bad, and then bought him a real pizza. I thought about that "
                 "all the way home.")]),
    S("s20", "woman in a sweater by a winter window", mood="reflective", ambience="room",
      characters=["Lena"],
      dialogue=[("Lena",
                 "The snow came early. I'm sleeping in your gray sweater, the one you said you lost at the "
                 "laundromat. I didn't lose it. I took it. It still smells a little like your studio, if I don't "
                 "breathe too hard. I'm not sorry. I hope you're warm. I also hope you're not.")]),
    S("s21", "young woman laughing close portrait", mood="reflective", ambience="room",
      characters=["Lena"],
      dialogue=[("Lena",
                 "I got stuck in a lift with a man who sang the whole of The Sound of Music. Then I bought a "
                 "goldfish, so the cabin would have a witness. His name is Rudolph. Don't say a word, Ethan. I "
                 "can hear exactly what your face is doing, and I miss it.")]),
    S("s22", "tired woman close up portrait", mood="reflective", ambience="room",
      characters=["Lena"],
      dialogue=[("Lena",
                 "I've been so tired. Not the good tired, from the kids. A heavy tired. I told myself it was the "
                 "winter and missing you, and missing you is real, but this is something else. I'm seeing a doctor "
                 "on Thursday. It's probably nothing. You know what I mean when I say probably. Worry a little.")]),
    S("s23", "worried young man close portrait", mood="reflective", ambience="night",
      characters=["Ethan"],
      narration="Probably was the word she saved for the opposite. I stopped the message. For the first time in three "
                "years I could see her. A plastic chair. A clock. Her hands folded so they wouldn't shake. And me, "
                "taking the fear out of strangers' voices, while I refused the only one that told me the truth."),
    S("s24", "worried woman in a hospital hallway", mood="reflective", ambience="room",
      characters=["Lena"],
      dialogue=[("Lena",
                 "I have the results. I'm not leaving them on a message you might erase before the verb. I need "
                 "one thing to stay mine for a few days. I'm all right. I'm not all right. Both of those are true "
                 "tonight. I'll call Maya. She picks up. That isn't a wound, Ethan. It's just true.")]),
    S("s25", "man and woman serious conversation portrait", mood="reflective", ambience="room",
      characters=["Maya", "Ethan"],
      dialogue=[("Maya", "You're listening."),
                ("Ethan", "Don't."),
                ("Maya", "You got as far as the doctor."),
                ("Ethan", "How do you know that?"),
                ("Maya", "Because your face just answered me. She made me promise the rest. Don't make me the villain for waiting in your kitchen while you caught up.")]),
    S("s26", "woman comforting a sad man", mood="reflective", ambience="room",
      characters=["Maya", "Ethan"],
      narration="In the spring she found a lump she couldn't stop checking. The surgeon was kind. The news was not. "
                "There was a treatment, and a number nobody in that kitchen would say out loud. Maya touched my "
                "arm. That was when I noticed I was shaking."),
    S("s27", "man and woman talking closely serious", mood="reflective", ambience="room",
      characters=["Maya", "Ethan"],
      dialogue=[("Maya", "She asked me not to let you drive up there out of guilt."),
                ("Ethan", "I'm not going to."),
                ("Maya", "Ethan."),
                ("Ethan", "What?"),
                ("Maya", "Your keys are in your hand. They've been in your hand since she said doctor.")]),
    S("s28", "hand holding a car key", mood="reflective", ambience="room",
      characters=["Ethan"],
      narration="I looked down. The key had cut a red line across my palm, and I hadn't felt it happen. I put the "
                "keys on the table. The little sound they made was the loudest thing in the apartment. Then I "
                "picked them up again, because I wasn't sure, and I was finished lying about the things I wasn't "
                "sure of."),

    S("s29", "thoughtful man on a bus by the window", mood="reflective", ambience="night",
      characters=["Ethan"],
      narration="I didn't skip. At the studio I listened with one ear and nodded like it was a mix note. On the bus "
                "I put my forehead on the glass. Outside the shop I stayed until the ice cream melted through the "
                "bag. I had been quick for three years. I was done."),
    S("s30", "pensive young woman portrait", mood="reflective", ambience="room",
      characters=["Lena"],
      dialogue=[("Lena",
                 "I'm going to stop calling and write letters instead. You always said my voice was too much on "
                 "the phone, too close, like I was standing in the room. Paper doesn't breathe. I think that's "
                 "the version of me you wanted. One that couldn't reach you.")]),
    S("s31", "young man headphones dark portrait", mood="reflective", ambience="night",
      characters=["Ethan"],
      narration="She didn't write the letters. A letter asks for an answer, and she already knew I wouldn't send "
                "one. A voicemail only asks for a witness. She knew I hadn't been one. She left them anyway, "
                "ordinary and unbearable, one day after another. Ordinary was the point. She was showing me the "
                "days I had refused to stand inside."),
    S("s32", "thoughtful young woman close portrait", mood="reflective", ambience="room",
      characters=["Lena"],
      dialogue=[("Lena",
                 "Rosa, in my class, asked what love is supposed to feel like. I said it's a voice you can still "
                 "hear when the room is empty and the dishes aren't done. She told me that was creepy. She may be "
                 "right. I hope you hear this one, Ethan, even on a day you think you don't deserve it. I'm tired "
                 "of being the one who decides that.")]),
    S("s33", "pensive woman close portrait", mood="reflective", ambience="wind",
      characters=["Lena"],
      dialogue=[("Lena",
                 "I'm going to say the thing I came up here not to say. I didn't leave because I stopped loving "
                 "you. I left because you were the only person who made me feel like the loudest thing in any room, "
                 "and I needed to know I could be quiet without disappearing.")]),
    S("s34", "woman alone on a wooden dock", mood="reflective", ambience="wind",
      characters=["Lena"],
      dialogue=[("Lena",
                 "I can be quiet. Some days I'm too quiet, and the cabin answers me back. Come up if you want to "
                 "hear me in a room again. Don't come because these messages made you feel rotten. Come because "
                 "you want to. You've always known the difference.")]),
    S("s35", "man staring at a phone in the dark", mood="reflective", ambience="night",
      lower="Nine days of nothing", characters=["Ethan"],
      narration="Then she stopped. Nine days. I didn't know she was in a hospital bed for most of them, counting "
                "tiles, choosing not to call, because the next voice I heard would have to be one I decided to "
                "hear. On a Tuesday, at three in the morning, the phone lit up. One new message. Her name. Number "
                "one thousand and ninety-five. I did not delete it."),
    S("s36", "young man driving at night", mood="reflective", ambience="night",
      lower="Tuesday, three in the morning", characters=["Ethan"],
      narration="I didn't call her back, and I didn't text my sister. I put on shoes that didn't match and I "
                "drove. The message sat in my pocket, unplayed, like something alive. I was afraid of the ending. "
                "I was more afraid of arriving without having chosen her. So I pointed the car at the mountains "
                "and I did not turn around."),
    S("s37", "snowy mountain road through a windshield", mood="reflective", ambience="wind",
      narration="Five hours, if you are sensible. I wasn't. I let a coffee go cold. I stopped when the snow took "
                "the white line, hazards blinking, until a road came back. Then I played her voice and shut it "
                "off before the last sentence. I wanted that sentence where she could see my face."),
    S("s38", "serious young man on the phone", mood="reflective", ambience="night",
      characters=["Maya", "Ethan"],
      dialogue=[("Maya", "Tell me you are not driving like a man in a film."),
                ("Ethan", "I'm driving like a man who waited three years."),
                ("Maya", "Did you hear the end of it?"),
                ("Ethan", "No."),
                ("Maya", "Good. Hear it next to her. And Ethan, arrive in one piece. She has had enough of people not arriving.")]),

    # One title card, on the cabin, not on a face. The music lifts here.
    S("s39", "wooden cabin beside a lake and pines", chapter="The porch", mood="hopeful", ambience="wind",
      narration="A mailbox shaped like a fish. Smoke past the pines. Her blue car, with the dent she never let me "
                "fix. I turned the engine off and my hand stayed on the door. It was the delete button again. "
                "This time she was on the other side of it."),
    S("s40", "smiling woman at a front door", mood="hopeful", ambience="wind",
      characters=["Lena", "Ethan"],
      dialogue=[("Lena", "You drove all this way to stand in the weather and stare at my car."),
                ("Ethan", "I was working up to the door."),
                ("Lena", "It's sleeting."),
                ("Ethan", "I know what sleet is."),
                ("Lena", "Then say it, and you can come in.")]),
    S("s41", "smiling woman in a winter hat close portrait", mood="hopeful", ambience="wind",
      characters=["Lena", "Ethan"],
      dialogue=[("Ethan", "It's sleet. And I brought the umbrella back."),
                ("Lena", "It is still the worst umbrella in Oregon."),
                ("Ethan", "It got me here."),
                ("Lena", "Hi, Ethan."),
                ("Ethan", "Hi, Lena.")]),
    S("s42", "young couple face to face portrait", mood="hopeful", ambience="wind",
      characters=["Ethan", "Lena"],
      dialogue=[("Ethan", "That shape on the water. That's the loon."),
                ("Lena", "That's Dennis. Six in the morning, whether or not I slept. Very dignified."),
                ("Ethan", "You named a wild bird Dennis."),
                ("Lena", "He has the face of a Dennis. Don't argue with the field work.")]),
    S("s43", "young couple drinking coffee together", mood="hopeful", ambience="room",
      characters=["Lena", "Ethan"],
      dialogue=[("Lena", "The treatment is working. For now. It also feels like my own car ran me over and then apologised. Nobody knows how long for now is. I'm not asking you to fix any of it."),
                ("Ethan", "I had a speech in the car. Sorry, four times, and a plan with steps."),
                ("Lena", "Leave it in the car. Be here. That is the whole job. It always was.")]),
    S("s44", "couple sitting close and talking quietly", mood="hopeful", ambience="room",
      characters=["Ethan", "Lena"],
      dialogue=[("Ethan", "I listened to all of them."),
                ("Lena", "I know."),
                ("Ethan", "Maya called you."),
                ("Lena", "She said you sat on the bathroom floor all night and refused coffee like it had insulted you. I cried. Then I got proud of you, which annoyed me. I wasn't ready to be proud.")]),
    S("s45", "sad man sitting on a bathroom floor", mood="hopeful", ambience="room",
      characters=["Ethan"],
      narration="Eleven hours, my back against the tub. I laughed at the goldfish and the pizza. I cried at the "
                "sweater, and at Rosa, and at the way she said my name when she thought nobody would press play. "
                "I deleted nothing. My thumb didn't even hover."),
    S("s46", "woman laughing in the sunlight close", mood="hopeful", ambience="wind",
      characters=["Lena", "Ethan"],
      dialogue=[("Lena", "Darnell is a nurse now. He sent a photo of a cake shaped like a cell. Pepperoni and all."),
                ("Ethan", "That is the worst cake I have ever heard described."),
                ("Lena", "I cried, and then I got proud of him. It's going around. Don't fight it.")]),
    S("s47", "couple standing on a dock", mood="hopeful", ambience="wind",
      characters=["Ethan", "Lena"],
      narration="We went down to the dock and we didn't talk for a while. Silence used to frighten me. It didn't, "
                "with her shoulder against mine. The mist came off the lake in slow sheets. Somewhere out past the "
                "dark, Dennis called once. Not a laugh. A check. He wanted to know if we were still there. We were."),
    S("s48", "young couple close up faces", mood="hopeful", ambience="wind",
      characters=["Lena", "Ethan"],
      dialogue=[("Lena", "Play the last one."),
                ("Ethan", "You know what it says. You said it."),
                ("Lena", "I want to watch your face for the part you wouldn't finish in the car. Play it. I'm right here. That's the difference.")]),
    S("s49", "couple looking at a smartphone", mood="hopeful", ambience="wind",
      characters=["Lena", "Ethan"],
      dialogue=[("Lena",
                 "Ethan. It's me. Obviously. If you're hearing this, you pressed play, and I am so glad. I'm at "
                 "the cabin. I'm sick, and I won't dress that up for you. I need you to hear it in my voice, not "
                 "in Maya's, and not in a silence you can still delete.")]),
    S("s50", "man and woman sitting close at sunrise", mood="hopeful", ambience="wind",
      characters=["Lena", "Ethan"],
      dialogue=[("Lena",
                 "Don't come because a message made you guilty. Guilt is just fear in a nicer coat, and you have "
                 "worn it long enough. Come because you want the morning. Dennis will be on the water at six. He "
                 "is very dignified. I love you. I didn't stop when I was loud, and I didn't stop when I went quiet.")]),
    S("s51", "couple hugging close sunset", mood="hopeful", ambience="wind",
      characters=["Ethan", "Lena"],
      dialogue=[("Ethan", "I want the morning."),
                ("Lena", "Then we start with this one. We don't count the others until they arrive."),
                ("Ethan", "I'm not deleting this one."),
                ("Lena", "I know. Your thumb looks retired.")]),
    S("s52", "couple hugging at sunset pier", beat="outro", mood="hopeful", ambience="wind",
      characters=["Ethan", "Lena"],
      narration="I don't know how many mornings a person gets. I know the name of the one I'm standing in. She is "
                "here, the bird has a ridiculous name, and I am not going anywhere that asks me to press delete. "
                "If you stayed, you heard her too. That's the whole thing. Someone, finally, heard her."),
]
