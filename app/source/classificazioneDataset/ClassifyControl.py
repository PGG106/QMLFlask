import os.path
import time
import csv
import pathlib
import warnings

import numpy as np
import pandas as pd
from qiskit import IBMQ
from qiskit.providers.ibmq import least_busy
from qiskit.aqua import QuantumInstance, aqua_globals
from qiskit.aqua.algorithms import QSVM
from qiskit.aqua.components.multiclass_extensions import AllPairs
from qiskit.circuit.library import ZZFeatureMap
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email.utils import formatdate
from email import encoders
from app.source.utils import utils
from app import app
from flask import request

warnings.simplefilter(action="ignore", category=DeprecationWarning)


@app.route("/classify_control", methods=["POST"])
# @login_required
def classify_control():
    """

    :return:
    """
    path_train = request.form.get("pathTrain")
    path_test = request.form.get("pathTest")
    path_prediction = request.form.get("userpathToPredict")
    features = request.form.getlist("features")
    token = request.form.get("token")
    backend = request.form.get("backend")
    email = request.form.get("email")

    result: dict = classify(
        path_train, path_test, path_prediction, features, token, backend
    )
    if result != 0:
        get_classified_dataset(result, path_prediction, email)

        # if result==0 token is not valid
        # if result==1 error on IBM server (error reported through email)
        # if result["noBackend"]==True selected backend is not active for the token or the are no active by default, and simulator is used
        # aggiungere controlli per result["noBackend"]==True e result==0 per
        # mostrare gli errori tramite frontend
    return "result"


def classify(
    path_train,
    path_test,
    user_path_to_predict,
    features,
    token,
    backend_selected,
):
    """

    :param path_train: path del file di training output delle fasi precedenti
    :param path_test: path del file di testing output delle fasi precedenti
    :param user_path_to_predict: path del file di prediction output delle fasi precedenti
    :param features: lista di features per qsvm
    :param token: token dell'utente
    :param backend_selected: backend selezionato dal form(se vuoto utilizza backend di default)
    :return: dict contenente informazioni relative alla classificazione
    """

    start_time = time.time()
    no_backend = False

    try:
        IBMQ.enable_account(token)
    except BaseException:
        print("Token not valid")
        return 0

    provider = IBMQ.get_provider(hub="ibm-q")
    IBMQ.disable_account()
    qubit = len(features)

    try:
        if (
            backend_selected
            and provider.get_backend(backend_selected).configuration().n_qubits
            >= qubit
        ):
            print("backend selected:" + str(backend_selected))
            print(
                "backend qubit:"
                + str(
                    provider.get_backend(backend_selected)
                    .configuration()
                    .n_qubits
                )
            )
            backend = provider.get_backend(
                backend_selected
            )  # Specifying Quantum System
        else:
            backend = least_busy(
                provider.backends(
                    filters=lambda x: x.configuration().n_qubits >= qubit
                    and not x.configuration().simulator
                    and x.status().operational
                )
            )
            print("least busy backend: ", backend)
            print(
                "backend qubit:"
                + str(
                    provider.get_backend(backend.name())
                    .configuration()
                    .n_qubits
                )
            )
    except BaseException:
        # when selected backend has not enough qubit, or no backends has enough
        # qubits, or the user token has no privileges to use the selected
        # backend
        no_backend = True
        backend = provider.get_backend("ibmq_qasm_simulator")
        print("backend selected: simulator")
        print(
            "backend qubit:"
            + str(
                provider.get_backend(backend.name()).configuration().n_qubits
            )
        )

    seed = 8192
    shots = 1024
    aqua_globals.random_seed = seed

    training_input, test_input = load_dataset(
        path_train, path_test, features, label="labels"
    )

    path_do_prediction = pathlib.Path(user_path_to_predict).parent
    if os.path.exists(path_do_prediction / "doPredictionFE.csv"):
        path_do_prediction = path_do_prediction / "doPredictionFE.csv"
    else:
        path_do_prediction = user_path_to_predict
    file_to_predict = open(path_do_prediction.__str__(), "r")
    prediction = np.array(
        list(csv.reader(file_to_predict, delimiter=","))
    ).astype("float")

    feature_map = ZZFeatureMap(
        feature_dimension=qubit, reps=2, entanglement="linear"
    )
    print(feature_map)

    qsvm = QSVM(
        feature_map,
        training_input,
        test_input,
        prediction,
        multiclass_extension=AllPairs(),
    )

    quantum_instance = QuantumInstance(
        backend,
        shots=shots,
        seed_simulator=seed,
        seed_transpiler=seed,
    )

    print("Running....\n")
    try:
        result = qsvm.run(quantum_instance)
    except BaseException:
        print("Error on IBM server")
        result = 1
        return result

    total_time = time.time() - start_time
    result["total_time"] = str(total_time)[0:6]

    print("Prediction from datapoints set:")
    for k, v in result.items():
        print("{} : {}".format(k, v))

    predicted_labels = result["predicted_labels"]

    classified_file = open(
        pathlib.Path(user_path_to_predict).parent / "classifiedFile.csv",
        "w",
    )
    prediction_file = open(user_path_to_predict, "r")
    rows = prediction_file.readlines()

    for j in range(1, utils.numberOfColumns(user_path_to_predict) + 1):
        classified_file.write("feature" + str(j) + ",")
    classified_file.write("label\n")
    i = 0
    for row in rows:
        classified_file.write(
            row.rstrip("\n") + "," + str(predicted_labels[i]) + "\n"
        )
        i += 1
    classified_file.close()
    prediction_file.close()
    file_to_predict.close()
    if no_backend:
        result["no_backend"] = True
    return result


def plot(classified_dataset):
    return classified_dataset


def load_dataset(training_path, testing_path, features, label):
    """
    Loads the data, normalizes it and returns it in the following format:
    {class_0: points_0, class_1:points_1, ...}
    Where points_i corresponds to the points that belong to class_i as a numpy array
    """
    df_train = pd.read_csv(training_path, index_col=0)
    df_test = pd.read_csv(testing_path, index_col=0)

    train, test = df_train, df_test

    train_dict, test_dict = {}, {}
    for category in train[label].unique():
        train_dict[category] = train[train["labels"] == category][
            features
        ].values
        test_dict[category] = test[test["labels"] == category][
            features
        ].values

    return train_dict, test_dict


def get_classified_dataset(result, userpathToPredict, email):
    """

    :param result: dict risultante dalla funzione classify dal quale si prendono i dati da inviare per email
    :return: 0 error, 1 done
    """

    msg = MIMEMultipart()
    msg["From"] = "quantumoonlight@gmail.com"
    msg["To"] = "quantumoonlight@gmail.com, " + email
    msg["Date"] = formatdate(localtime=True)
    # + dataset.name + " " + dataset.upload_date
    msg["Subject"] = "Classification Result "

    if result == 1:
        msg.attach(
            MIMEText(
                "IBM Server error, please check status on https://quantum-computing.ibm.com/services?services=systems\n"
            )
        )
    else:
        msg.attach(MIMEText("This is your classification:\n\n"))
        accuracy = result.get("testing_accuracy")
        success_ratio = result.get("test_success_ratio")
        msg.attach(
            MIMEText("Testing accuracy: " + "{:.2%}".format(accuracy) + "\n")
        )
        msg.attach(
            MIMEText("Success ratio: " + "{:.2%}".format(success_ratio) + "\n")
        )
        msg.attach(
            MIMEText("Total time elapsed:" + result.get("total_time") + "s")
        )

        # file = pathlib.Path(session["datasetPath"] / "classifiedFile.csv"
        file = pathlib.Path(userpathToPredict).parent / "classifiedFile.csv"
        attach_file = open(file, "rb")
        payload = MIMEBase("application", "octet-stream")
        payload.set_payload(attach_file.read())
        encoders.encode_base64(payload)
        payload.add_header(
            "Content-Disposition",
            "attachment",
            filename="ClassifiedDataset.csv",
        )
        msg.attach(payload)
        attach_file.close()

    try:
        server = smtplib.SMTP_SSL("smtp.gmail.com", 465)
        server.ehlo()
        server.login("quantumoonlight@gmail.com", "Quantum123?")
        server.send_message(msg)
        server.close()
    except BaseException:
        return 0
    return 1