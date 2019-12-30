"""

"yolo/yolo.py"

Class definition of YOLO_v3 style detection model on image and video

"""

import cv2
import keras
from keras import backend as K
import numpy as np
import tensorflow as tf

from utils.datagen import data_generator
from yolo.head.backend import yolo_eval, yolo_loss
from yolo.head.join import yolo


# ---------------- YOLO! ----------------
class YOLO:
    """YOLO as a class"""

    HYPERPARAMS = {
        "img_size": (416, 416),
        "score": 0.3,
        "iou": 0.45,
    }

    BACKBONES = [
        "x-net",
        "darknet",
    ]


    # INITS
    def __init__(self, model_path, anchors_path, classes_path, backbone="x-net", sess=None, **kwargs):
        self.HYPERPARAMS.update(kwargs)

        self.sess = sess
        self.model_path = model_path
        self.anchors_path = anchors_path
        self.classes_path = classes_path
        self.backbone = backbone

        self.sess_init()
        self.anchor_and_cls_init()
        self.model_init()

    def anchor_and_cls_init(self):
        with open(self.anchors_path) as anchor_file:
            self.anchors = np.array(anchor_file.readline().split(","), dtype=np.float32).reshape(-1, 2)
        with open(self.classes_path) as classes_file:
            self.classes = [cls.strip() for cls in classes_file]

    def model_init(self):
        assert self.model_path.endswith(".h5"), "only keras .h5 files supported"
        assert self.backbone in self.BACKBONES, "supported backbones are {}".format(self.BACKBONES)

        inputs = keras.layers.Input((*self.HYPERPARAMS["img_size"], 3))
        self.yolo = yolo(inputs, len(self.anchors) // 3, len(self.classes), backbone_type=self.backbone)
        self.yolo.load_weights(self.model_path, by_name=True, skip_mismatch=True)
        print("{} loaded".format(self.model_path))

        self.img_size_tensor = K.placeholder(shape=(2,))
        self.bounding_boxes, self.scores, self.predicted_classes = yolo_eval(
            yolo_outputs=self.yolo.output,
            anchors=self.anchors,
            num_classes=len(self.classes),
            image_shape=self.img_size_tensor,
            score_threshold=self.HYPERPARAMS["score"],
            iou_threshold=self.HYPERPARAMS["iou"]
        )

    def sess_init(self):
        if self.sess is None:
            config = tf.ConfigProto(gpu_options=tf.GPUOptions(allow_growth=True))
            K.set_session(tf.Session(config=config))
            self.sess = K.get_session()
        else:
            K.set_session(self.sess)


    # TRAINING UTILS
    @staticmethod
    def freeze(yolo, mode):
        """Freezes a yolo model

        :param yolo: yolo as keras model
        :param mode: either "full train" (freeze none) or "finetune" (freeze all but head and branches)

        """

        if mode == "full train":
            for layer in yolo.layers:
                layer.trainable = True
        elif mode == "finetune":
            for layer in yolo.layers:
                if "branch" in layer.name or "yolo" in layer.name:
                    layer.trainable = True
                else:
                    layer.trainable = False
        else:
            raise ValueError("mode is either 'full train' or 'finetune'")


    # TRAINING
    def prepare_for_training(self, freeze=None, optimizer=keras.optimizers.Adam(1e-4), *args, **kwargs):
        """Makes and compiles the yolo training model (adds lambda loss)

        :param freeze: freeze function. Recommended to use YOLO.freeze
        :param optimizer: keras optimizer
        :param args: additional params for freeze. YOLO.freeze requires one parameter: "mode"
        :param kwargs: additional params for freeze

        """

        # freeze layers
        if freeze is None:
            freeze = self.freeze(self.yolo, mode="full train")
        freeze(self.yolo, *args, **kwargs)

        frozen, trainable = 0, 0
        for layer in self.yolo.layers:
            if layer.trainable:
                trainable += 1
            else:
                frozen += 1
        print("{} layers out of {} are trainable".format(trainable, frozen + trainable))

        # add lambda loss
        height, width = self.HYPERPARAMS["img_size"]
        heights_and_widths = [32, 16, 8]

        y_true = [keras.layers.Input((height // h_or_w, width // h_or_w, len(self.anchors) // 3, len(self.classes) + 5))
                  for h_or_w in heights_and_widths]

        loss_layer = keras.layers.Lambda(
            yolo_loss,
            output_shape=(1,),
            name="yolo_loss",
            arguments={
                "anchors": self.anchors,
                "num_classes": len(self.classes),
                "ignore_thresh": self.HYPERPARAMS["score"]  # 0.5
            }
        )([*self.yolo.output, *y_true])

        # make and compile
        self._yolo_train = keras.Model([self.yolo.input, *y_true], loss_layer)

        self._yolo_train.compile(optimizer, loss=lambda y_true, y_pred: y_pred)

    def train(self, annotation_path, save_path, epochs=1, batch_size=1, val_split=0.1, callbacks=None):
        """Train a yolo model

        :param annotation_path: path to annotations file
        :param save_path: path to which to save the weights of the model
        :param epochs: number of epochs to train for
        :param batch_size: batch size
        :param val_split: decimal percent of data used for validaiton
        :param callbacks: list of keras callbacks objects
        :returns: keras History object

        """

        # get annotations
        with open(annotation_path, "r") as annotation_file:
            annotations = annotation_file.readlines()

        np.random.seed(10101)
        np.random.shuffle(annotations)
        np.random.seed(None)

        # set up training
        num_validation = int(len(annotations) * val_split)
        num_train = len(annotations) - num_validation

        print("Epochs: {}\nBatch size: {}\nTrain: {} samples\nValidation: {} samples".format(
            epochs, batch_size, num_train, num_validation))

        # train
        history = self._yolo_train.fit_generator(
            generator=data_generator(
                annotations[:num_train],
                batch_size,
                self.HYPERPARAMS["img_size"],
                self.anchors,
                len(self.classes)
            ),
            steps_per_epoch=max(1, num_train // batch_size),
            validation_data=data_generator(
                annotations[num_train:],
                batch_size,
                self.HYPERPARAMS["img_size"],
                self.anchors,
                len(self.classes)
            ),
            validation_steps=max(1, num_validation // batch_size),
            epochs=epochs,
            callbacks=callbacks
        )

        self._yolo_train.save_weights(save_path)

        # transfer trained weights to yolo model
        self.yolo.set_weights(self._yolo_train.get_weights())
        del self._yolo_train

        return history


    # DETECTION
    def detect(self, img):
        """Detects objects in image

        :param img: image as array with shape (h, w, 3)
        :returns: boxes, scores, classes

        """

        original_shape = img.shape[:2]

        img = img.astype(np.float32) / 255.
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        img = cv2.resize(img, self.HYPERPARAMS["img_size"])
        img = np.expand_dims(img, axis=0)

        return self.sess.run(
            [self.bounding_boxes, self.scores, self.predicted_classes],
            feed_dict={
                self.yolo.input: img,
                self.img_size_tensor: original_shape,
                K.learning_phase(): 0
            }
        )
